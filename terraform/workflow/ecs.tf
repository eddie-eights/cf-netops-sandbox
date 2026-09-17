# ---------------------------------------------------------------- network
resource "aws_security_group" "task" {
  name        = "${var.name_prefix}-workflow"
  description = "Workflow task - HTTPS to the VPC endpoints (ECR, logs, DynamoDB gateway, SSM, AgentCore, SQS)"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name_prefix}-workflow" }
}

resource "aws_vpc_security_group_egress_rule" "task_https" {
  security_group_id = aws_security_group.task.id
  description       = "ECR / logs / DynamoDB / SSM / bedrock-agentcore / sqs endpoints"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_task" {
  security_group_id            = local.endpoint_sg_id
  description                  = "Workflow task through the endpoints of terraform/main"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.task.id
}

# ---------------------------------------------------------------- cluster / logs
resource "aws_ecs_cluster" "workflow" {
  name = "${var.name_prefix}-workflow"

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
  family                   = "${var.name_prefix}-workflow"
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
        { containerPort = 8233, protocol = "tcp" }, # Web UI（README workflow-3 のポートフォワード）
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
        { name = "ANOMALY_TABLE", value = local.anomaly_table },
        { name = "ANOMALY_QUEUE_URL", value = aws_sqs_queue.anomalies.url },
        { name = "PROPOSAL_TABLE", value = aws_dynamodb_table.proposals.name },
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
      error_message = "terraform/ecr の state から worker_repository_url / temporal_repository_url が読めない。terraform/ecr を create_workflow_repositories = true で apply する。"
    }
  }
}

resource "aws_ecs_service" "workflow" {
  name            = "${var.name_prefix}-workflow"
  cluster         = aws_ecs_cluster.workflow.id
  task_definition = aws_ecs_task_definition.workflow.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # aws ecs execute-command でタスクの中に入れる（README workflow-3）
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
