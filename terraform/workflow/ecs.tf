# ---------------------------------------------------------------- network
# タスクの SG は terraform/base/core の workflow。受信は Web の EC2 からの Temporal UI の 8233（SSM のポートフォワーディング。docs/workflow.md
# 「Temporal UI を開く」）だけ、送信は Neptune の 8182 とエンドポイントと S3 の 443（security_groups.tf の通信の表）。
# Temporal の gRPC（7233）はタスクの中の localhost だけで待つ（ワーカーは同じタスク。下の --ip 127.0.0.1）。
# ECR / logs / SSM / AgentCore / SQS / s3tables へは terraform/base/core のインターフェース型エンドポイント（ops/up.sh が WORKFLOW のときに作らせる）を通る。
# 2026-09-26 まではここにタスクの SG と 7 本のルールがあった（7c42b0f）

# ---------------------------------------------------------------- cluster / logs
resource "aws_ecs_cluster" "workflow" {
  name = "${local.name_prefix}-workflow"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_cloudwatch_log_group" "workflow" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------- task definition (Temporal dev server + worker in one task)
resource "aws_ecs_task_definition" "workflow" {
  family                   = "${local.name_prefix}-workflow"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "temporal"
      image     = local.temporal_image
      essential = true
      # temporalio/temporal の entrypoint は `temporal`（CLI）。start-dev は SQLite を /tmp に置く（タスクが消えると消える）。
      # gRPC（7233）と HTTP・メトリクスのポートは --ip の 127.0.0.1（同じタスクのワーカーだけが localhost でつなぐ）、Web UI だけ --ui-ip で外に出す
      # （UI は同じコンテナの中から 127.0.0.1:7233 を読む）。2026-09-29 までは --ip 0.0.0.0 で 7233 もタスクの外に開いていた
      command = ["server", "start-dev", "--ip", "127.0.0.1", "--ui-ip", "0.0.0.0", "--db-filename", "/tmp/temporal.db", "--log-level", "warn"]
      portMappings = [
        { containerPort = 8233, protocol = "tcp" }, # Web UI（docs/workflow.md「Temporal UI を開く」）
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.workflow.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "temporal"
        }
      }
    },
    {
      name      = "worker"
      image     = local.worker_image
      essential = true
      dependsOn = [{ containerName = "temporal", condition = "START" }]
      environment = [
        { name = "TEMPORAL_ADDRESS", value = "localhost:7233" },
        { name = "AWS_REGION", value = var.region },
        { name = "PARAM_PREFIX", value = local.param_prefix },
        { name = "ANOMALY_QUEUE_URL", value = aws_sqs_queue.anomalies.url },
        { name = "NEPTUNE_ENDPOINT", value = local.neptune_endpoint },
        { name = "AUDIT_TABLE_BUCKET_ARN", value = local.audit_bucket_arn },
        { name = "AUDIT_NAMESPACE", value = local.audit_namespace },
        { name = "PROPOSAL_EVENTS_TABLE", value = local.proposal_events_table_name },
        { name = "AGENT_RUNTIME_ARN", value = local.runtime_arn },
        { name = "LAB_INSTANCE_ID", value = local.lab_instance_id },
        { name = "APPROVAL_TIMEOUT_MINUTES", value = tostring(var.approval_timeout_minutes) },
        { name = "VERIFY_TIMEOUT", value = tostring(var.verify_timeout_seconds) },
        { name = "HOLD_MINUTES", value = tostring(var.hold_minutes) },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.workflow.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "worker"
        }
      }
    },
  ])

  lifecycle {
    precondition {
      condition     = local.worker_repository_url != "" && local.temporal_repository_url != ""
      error_message = "terraform/base/ecr の state から worker_repository_url / temporal_repository_url が読めない。terraform/base/ecr を create_workflow_repositories = true で apply する。"
    }
    precondition {
      condition     = local.runtime_arn != ""
      error_message = "terraform/agent の state から agent_runtime_arn が読めない。terraform/agent を先に apply する（deploy.env の AGENT=1）。"
    }
    precondition {
      condition     = local.neptune_endpoint != "" && local.neptune_data_arn != ""
      error_message = "terraform/pipeline/graph の state から cluster_endpoint / cluster_resource_id が読めない。修復案の「いま」は Neptune にあるので、terraform/pipeline/graph を先に apply する（2026-09-24 から）。"
    }
    precondition {
      condition     = local.audit_bucket_arn != "" && local.audit_namespace != "" && local.proposal_events_table_name != ""
      error_message = "terraform/pipeline/analytics の state から table_bucket_arn / table_namespace / proposal_events_table_name が読めない。修復案の証跡は S3 Tables の proposal_events に書くので、terraform/pipeline/analytics を先に apply する（2026-09-24 から）。"
    }
  }
}

resource "aws_ecs_service" "workflow" {
  name            = "${local.name_prefix}-workflow"
  cluster         = aws_ecs_cluster.workflow.id
  task_definition = aws_ecs_task_definition.workflow.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # aws ecs execute-command でタスクの中に入れる
  enable_execute_command = true

  # 2 つ同時に立てない（SQLite はタスクの中）
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = [local.subnet_id]
    security_groups  = [local.workflow_sg_id]
    assign_public_ip = false
  }

  depends_on = [aws_iam_role_policy.task, aws_iam_role_policy_attachment.execution]
}
