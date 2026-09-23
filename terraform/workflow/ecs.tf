# ---------------------------------------------------------------- network
resource "aws_security_group" "task" {
  name        = "${local.name_prefix}-workflow"
  description = "Workflow task - HTTPS to the VPC endpoints (ECR, logs, S3 gateway, s3tables, SSM, AgentCore, SQS) and 8182 to Neptune"
  vpc_id      = local.vpc_id

  tags = { Name = "${local.name_prefix}-workflow" }
}

resource "aws_vpc_security_group_egress_rule" "task_https" {
  security_group_id = aws_security_group.task.id
  description       = "ECR / logs / S3 gateway / s3tables / SSM / bedrock-agentcore / sqs endpoints"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_task" {
  security_group_id            = local.endpoint_sg_id
  description                  = "Workflow task through the endpoints of terraform/base/core"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.task.id
}

# 異常と修復案の「いま」を Neptune（terraform/pipeline/graph）に読み書きする。Neptune の SG はあちらのルートのものだが、相手のタスクの SG がこのルートにあるので、ここで足す
resource "aws_vpc_security_group_egress_rule" "task_neptune" {
  count = local.neptune_sg_id != "" ? 1 : 0

  security_group_id            = aws_security_group.task.id
  description                  = "Gremlin to Neptune (terraform/pipeline/graph)"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = local.neptune_sg_id
}

resource "aws_vpc_security_group_ingress_rule" "neptune_from_task" {
  count = local.neptune_sg_id != "" ? 1 : 0

  security_group_id            = local.neptune_sg_id
  description                  = "Workflow task of terraform/workflow"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = aws_security_group.task.id
}

# Temporal UI（8233）は Web の EC2 を踏み台にした SSM のポートフォワーディングで開く（docs/workflow.md「Temporal UI を開く」）。
# 踏み台の Web の SG から出て、タスクの SG に入る 2 本だけ開ける（どちらも相手の SG に限る。無いとセッションはつながっても UI が開かない）。
# Web の SG は terraform/base/core のものだが、相手のタスクの SG がこのルートにあるので、ここで足す（gateway.tf の neptune_from_tools と同じ形）
resource "aws_vpc_security_group_ingress_rule" "task_ui_from_web" {
  security_group_id            = aws_security_group.task.id
  description                  = "Temporal UI from the chat web EC2 (SSM port forwarding)"
  ip_protocol                  = "tcp"
  from_port                    = 8233
  to_port                      = 8233
  referenced_security_group_id = local.web_sg_id
}

resource "aws_vpc_security_group_egress_rule" "web_to_task_ui" {
  security_group_id            = local.web_sg_id
  description                  = "Temporal UI of the workflow task (SSM port forwarding)"
  ip_protocol                  = "tcp"
  from_port                    = 8233
  to_port                      = 8233
  referenced_security_group_id = aws_security_group.task.id
}

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
      # temporalio/temporal の entrypoint は `temporal`（CLI）。start-dev は SQLite を /tmp に置く（タスクが消えると消える）
      command = ["server", "start-dev", "--ip", "0.0.0.0", "--db-filename", "/tmp/temporal.db", "--log-level", "warn"]
      portMappings = [
        { containerPort = 7233, protocol = "tcp" }, # gRPC（ワーカー）
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
        { name = "POLL_INTERVAL", value = tostring(var.poll_interval_seconds) },
        { name = "APPROVAL_TIMEOUT_MINUTES", value = tostring(var.approval_timeout_minutes) },
        { name = "VERIFY_ATTEMPTS", value = tostring(var.verify_attempts) },
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
      condition     = local.neptune_endpoint != "" && local.neptune_data_arn != "" && local.neptune_sg_id != ""
      error_message = "terraform/pipeline/graph の state から cluster_endpoint / cluster_resource_id / neptune_security_group_id が読めない。異常と修復案の「いま」は Neptune にあるので、terraform/pipeline/graph を先に apply する（2026-09-24 から）。"
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
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  depends_on = [aws_iam_role_policy.task, aws_iam_role_policy_attachment.execution]
}
