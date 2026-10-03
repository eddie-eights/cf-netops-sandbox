# ---------------------------------------------------------------- Telegraf (ECS on Fargate + internal NLB)
# 機器の SNMP のポーリング・gNMI の購読・trap・syslog を受けて MSK に書く Telegraf を、Fargate のタスク 1 つで動かす（2026-09-28 まで terraform/pipeline/lab の EC2）。
# イメージは telegraf/Dockerfile（公式の telegraf に設定のテンプレートと入口を足したもの）で、ops/up.sh が ECR の <接頭辞>-telegraf に置く。
# lab の管理ネットワーク（203.0.113.0/24）は lab の EC2 の中の docker network なので、terraform/pipeline/lab（forward_to_telegraf）が VPC のルートと
# lab.sh forward で届ける:
#   ポーリング  タスク → 機器の SNMP（161/udp）と gNMI（57400/tcp）。送り元はタスクの IP で、作り直すたびに変わるので、lab.sh forward は
#               タスクのサブネットの CIDR（SSM の /<接頭辞>/telegraf-source-cidr）で通す
#   trap        機器 → lab の EC2 の 162/udp → DNAT → 下の NLB の 162 → タスクの 1162（非 root は 1024 未満で待てない）
#   syslog      機器 → lab の EC2 の 5140/udp → DNAT → NLB の 5140 → タスクの 5140
# タスクの IP は作り直すと変わるので、DNAT の宛先は変わらない NLB の IP にする（SSM の /<接頭辞>/telegraf-address）。
# NLB は送り元の IP を残す（UDP のターゲットは client IP preservation が既定で、Spark とエージェントは送り元の IP で機器を引く）。
# SG は NLB（telegraf_nlb）とタスク（telegraf）で別々で、ルールは terraform/base/core の security_groups.tf の通信の表にある:
#   NLB   管理ネットワークの CIDR から udp 162 / 5140 を受け（送り元が機器の管理 IP のまま）、タスクの SG へ udp 1162 / 5140 と tcp 8080（ヘルスチェック）を送る
#   タスク NLB の SG から受け（送り元の IP が残っても、NLB の SG を参照したルールで通る）、MSK の 9098・管理ネットワークの udp 161 / tcp 57400・
#         エンドポイントと S3 の 443 へ送る

locals {
  telegraf_repository_url = try(data.terraform_remote_state.ecr.outputs.telegraf_repository_url, "")
  telegraf_image          = "${local.telegraf_repository_url}:${var.telegraf_image_tag}"
  telegraf_log_group      = "/ecs/${local.name_prefix}-telegraf"

  # NLB の受け口 → タスクのポート（telegraf/telegraf.conf.in の inputs.snmp_trap と inputs.syslog）
  telegraf_ports = {
    trap   = { listener = 162, container = 1162 }
    syslog = { listener = 5140, container = 5140 }
  }
}

data "aws_subnet" "telegraf" {
  id = local.telegraf_subnet_id
}

# ---------------------------------------------------------------- NLB
resource "aws_lb" "telegraf" {
  name               = "${local.name_prefix}-tg"
  internal           = true
  load_balancer_type = "network"
  subnets            = [local.telegraf_subnet_id]
  # NLB の SG は作るときにしか付けられない（後から足すと作り直し。付けて作った NLB なら入れ替えはできる）
  security_groups = [local.telegraf_nlb_sg_id]

  tags = { Name = "${local.name_prefix}-telegraf" }
}

# NLB のアドレス（サブネット 1 つなので ENI も 1 つ）。lab.sh forward の DNAT の宛先
data "aws_network_interface" "telegraf_lb" {
  filter {
    name   = "description"
    values = ["ELB ${aws_lb.telegraf.arn_suffix}"]
  }
  filter {
    name   = "vpc-id"
    values = [local.vpc_id]
  }
}

resource "aws_lb_target_group" "telegraf" {
  for_each = local.telegraf_ports

  name        = "${local.name_prefix}-${each.key}"
  port        = each.value.container
  protocol    = "UDP"
  target_type = "ip"
  vpc_id      = local.vpc_id

  preserve_client_ip = true
  # タスクを作り直すとき、古いタスクを長く待たない
  deregistration_delay = 10

  # UDP は応答で生死を見られないので、Telegraf の outputs.health（telegraf.conf.in）を見る
  health_check {
    protocol            = "HTTP"
    port                = "8080"
    path                = "/"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = { Name = "${local.name_prefix}-telegraf-${each.key}" }
}

resource "aws_lb_listener" "telegraf" {
  for_each = local.telegraf_ports

  load_balancer_arn = aws_lb.telegraf.arn
  port              = each.value.listener
  protocol          = "UDP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.telegraf[each.key].arn
  }
}

resource "aws_ssm_parameter" "telegraf_address" {
  name        = "/${local.name_prefix}/telegraf-address"
  type        = "String"
  value       = data.aws_network_interface.telegraf_lb.private_ip
  description = "Private IP of the Telegraf NLB. Read by lab.sh forward on the lab EC2 (trap / syslog DNAT target)."
}

resource "aws_ssm_parameter" "telegraf_source_cidr" {
  name        = "/${local.name_prefix}/telegraf-source-cidr"
  type        = "String"
  value       = data.aws_subnet.telegraf.cidr_block
  description = "CIDR of the subnet of the Telegraf task (its IP changes on every replacement). Read by lab.sh forward on the lab EC2 (SNMP / gNMI polling source)."
}

# ---------------------------------------------------------------- ECS
resource "aws_ecs_cluster" "telegraf" {
  name = "${local.name_prefix}-telegraf"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_cloudwatch_log_group" "telegraf" {
  name              = local.telegraf_log_group
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "telegraf" {
  family                   = "${local.name_prefix}-telegraf"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.telegraf_task_cpu
  memory                   = var.telegraf_task_memory
  execution_role_arn       = aws_iam_role.telegraf_execution.arn
  task_role_arn            = aws_iam_role.telegraf_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  # ECS Exec（tg test / tg gnmi）を使うので readonlyRootFilesystem は付けない（ECS Exec が対応していない）。telegraf.sh が書くのは /tmp だけ
  container_definitions = jsonencode([
    {
      name      = "telegraf"
      image     = local.telegraf_image
      essential = true
      portMappings = [
        { containerPort = 1162, protocol = "udp" }, # trap（NLB の 162 から）
        { containerPort = 5140, protocol = "udp" }, # syslog
        { containerPort = 8080, protocol = "tcp" }, # outputs.health（NLB のヘルスチェック）
      ]
      environment = [
        { name = "AWS_REGION", value = var.region },
        { name = "KAFKA_BROKERS", value = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam },
        { name = "SNMP_AGENTS", value = var.snmp_agents },
        { name = "GNMI_TARGETS", value = var.gnmi_targets },
        { name = "SYSLOG_STANDARD", value = var.syslog_standard },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.telegraf.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "telegraf"
        }
      }
    },
  ])

  lifecycle {
    precondition {
      condition     = local.telegraf_repository_url != ""
      error_message = "terraform/base/ecr の state から telegraf_repository_url が読めない。terraform/base/ecr を先に apply する（ops/up.sh の手順 1）。"
    }
  }
}

resource "aws_ecs_service" "telegraf" {
  name            = "${local.name_prefix}-telegraf"
  cluster         = aws_ecs_cluster.telegraf.id
  task_definition = aws_ecs_task_definition.telegraf.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # aws ecs execute-command でタスクの中に入れる（tg test / tg gnmi。コマンドは output telegraf_exec_command）
  enable_execute_command = true

  # 2 つ同時に立てない（同じ機器を 2 回ポーリングして MSK に 2 回書かない）
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = [local.telegraf_subnet_id]
    security_groups  = [local.telegraf_sg_id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = local.telegraf_ports
    content {
      target_group_arn = aws_lb_target_group.telegraf[load_balancer.key].arn
      container_name   = "telegraf"
      container_port   = load_balancer.value.container
    }
  }

  depends_on = [
    aws_lb_listener.telegraf,
    aws_iam_role_policy.telegraf_task,
    aws_iam_role_policy_attachment.telegraf_execution,
  ]
}

# ---------------------------------------------------------------- IAM
data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "telegraf_execution" {
  name               = "${local.name_prefix}-telegraf-exec"
  description        = "ECS task execution role of the Telegraf task (ECR pull, CloudWatch Logs)"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "telegraf_execution" {
  role       = aws_iam_role.telegraf_execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "telegraf_task" {
  name               = "${local.name_prefix}-telegraf-task"
  description        = "Telegraf task - write SNMP / gNMI / trap / syslog to MSK (IAM auth), ECS Exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "telegraf_task" {
  name = "${local.name_prefix}-telegraf-task"
  role = aws_iam_role.telegraf_task.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Kafka"
        Effect = "Allow"
        Action = [
          "kafka-cluster:Connect",
          "kafka-cluster:DescribeCluster",
          "kafka-cluster:WriteData",
          "kafka-cluster:WriteDataIdempotently",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:CreateTopic",
        ]
        Resource = [aws_msk_cluster.stream.arn, local.topic_arns]
      },
      {
        # ECS Exec（aws ecs execute-command）
        Sid    = "EcsExec"
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      },
    ]
  })
}

# terraform/base/core の perimeter.tf の Deny（VPC エンドポイントを通らない呼び出しを拒む）。実行ロールとタスクロールの両方に付ける
resource "aws_iam_role_policy_attachment" "telegraf_execution_perimeter" {
  count = local.perimeter_policy_arn != "" ? 1 : 0

  role       = aws_iam_role.telegraf_execution.name
  policy_arn = local.perimeter_policy_arn
}

resource "aws_iam_role_policy_attachment" "telegraf_task_perimeter" {
  count = local.perimeter_policy_arn != "" ? 1 : 0

  role       = aws_iam_role.telegraf_task.name
  policy_arn = local.perimeter_policy_arn
}
