# ---------------------------------------------------------------- Splunk Enterprise（ECS on Fargate）
# sinks に splunk があるとき、Splunk の公式イメージ（splunk/splunk）にアラートの app を足したもの（splunk/Dockerfile）を 1 タスク立て、
# Spark の splunk の格納先が VPC の中の HEC（https://splunk.<名前空間>:8088）へ書く。AWS の外へは出ない（2026-09-28 まであった外の Splunk へ NAT で出る道はやめた）。
# イメージは amd64 しか無いので X86_64 のタスクにし、ops/up.sh の手順 2 が ECR の <接頭辞>-splunk に入れる（VPC から AWS の外へ出る経路が無いので Docker Hub から引けない）。
# 検知は app netops_alerts の保存済みサーチ（リンク・BGP・IS-IS・trap）で、アラートアクション netops_sns が土台の SNS トピック
# （terraform/base/core の alerts.tf）へ publish する。認証はタスクロール（アクセスキーは置かない）で、sns のエンドポイントを通る。
# ライセンスは Splunk Enterprise の試用（60 日、1 日 500 MB まで）。SPLUNK_START_ARGS / SPLUNK_GENERAL_TERMS で起動時に Splunk の
# ライセンスと Splunk General Terms に同意する（イメージがこの 2 つ無しでは起きない）ので、デプロイする人が同意したことになる。
# index はタスクのエフェメラルストレージにあり、タスクと一緒に消える（PoC。残すなら EFS が要る）。
# 管理者のパスワードと HEC の token は ops/up.sh が作る SSM の SecureString を secrets で受ける（値は Terraform も state も持たない）。
# UI は output splunk_port_forward_command（web の EC2 を踏み台にした SSM のポートフォワード。PoC 用）

locals {
  splunk_image        = "${try(data.terraform_remote_state.ecr.outputs.splunk_repository_url, "")}:${var.splunk_image_tag}"
  splunk_log_group    = "/ecs/${local.name_prefix}-splunk"
  splunk_password_arn = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${local.splunk_password_parameter}"
}

resource "aws_cloudwatch_log_group" "splunk" {
  count = local.splunk_on_ecs ? 1 : 0

  name              = local.splunk_log_group
  retention_in_days = var.log_retention_days
}

resource "aws_service_discovery_service" "splunk" {
  count = local.splunk_on_ecs ? 1 : 0

  name          = "splunk"
  force_destroy = true

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.analytics[0].id
    routing_policy = "MULTIVALUE"

    dns_records {
      ttl  = 10
      type = "A"
    }
  }
}

resource "aws_ecs_task_definition" "splunk" {
  count = local.splunk_on_ecs ? 1 : 0

  family                   = "${local.name_prefix}-splunk"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.splunk_task_cpu
  memory                   = var.splunk_task_memory
  execution_role_arn       = aws_iam_role.splunk_execution[0].arn
  # アラートアクション（splunk/netops_alerts/bin/netops_sns.py）が SNS へ publish する
  task_role_arn = aws_iam_role.splunk_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  ephemeral_storage {
    size_in_gib = var.splunk_ephemeral_storage_gib
  }

  container_definitions = jsonencode([
    {
      name      = "splunk"
      image     = local.splunk_image
      essential = true
      portMappings = [
        { containerPort = 8000, protocol = "tcp" }, # Web UI（http）
        { containerPort = 8088, protocol = "tcp" }, # HEC（https、イメージの自己署名）
        { containerPort = 8089, protocol = "tcp" }, # 管理 API（https）
      ]
      environment = [
        { name = "SPLUNK_START_ARGS", value = "--accept-license" },
        { name = "SPLUNK_GENERAL_TERMS", value = "--accept-sgt-current-at-splunk-com" },
        # アラートアクションが読む（splunk/entrypoint.sh がファイルに写す。splunkd の子プロセスはコンテナの環境変数を引き継がない）
        { name = "AWS_REGION", value = var.region },
        { name = "ALERTS_TOPIC_ARN", value = local.alerts_topic_arn },
        # gNMI と trap のイベントは機器の名前でなく管理 IP を持つ。アラートアクションがこの表で名前に直す
        { name = "DEVICE_MAP", value = var.device_map },
      ]
      secrets = [
        { name = "SPLUNK_PASSWORD", valueFrom = local.splunk_password_arn },
        # イメージがこの値で HEC の token（既定の index は main）を作る。Spark は同じ SSM の値を読んで送る
        { name = "SPLUNK_HEC_TOKEN", valueFrom = local.splunk_token_parameter_arn },
      ]
      # 起動（Ansible での初期設定）に数分かかる。ops/up.sh はこれが HEALTHY になってから Spark のジョブを出す
      # （ジョブの HEC への POST は再試行の後に落ちるので、Splunk が起きる前に出すとジョブが止まる）
      healthCheck = {
        command     = ["CMD-SHELL", "/sbin/checkstate.sh"]
        interval    = 30
        timeout     = 10
        retries     = 10
        startPeriod = 300
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.splunk[0].name
          awslogs-region        = var.region
          awslogs-stream-prefix = "splunk"
        }
      }
    },
  ])

  lifecycle {
    precondition {
      condition     = try(data.terraform_remote_state.ecr.outputs.splunk_repository_url, "") != ""
      error_message = "terraform/base/ecr の state から splunk_repository_url が読めない。terraform/base/ecr を先に apply する（ops/up.sh の手順 1）。"
    }
    precondition {
      condition     = local.alerts_topic_arn != ""
      error_message = "terraform/base/core の state から alerts_topic_arn が読めない（2026-10-02 より前の土台）。terraform/base/core を先に apply する。"
    }
  }
}

resource "aws_ecs_service" "splunk" {
  count = local.splunk_on_ecs ? 1 : 0

  name            = "${local.name_prefix}-splunk"
  cluster         = aws_ecs_cluster.analytics[0].id
  task_definition = aws_ecs_task_definition.splunk[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # index はタスクの中にしか無いので 2 つ同時に立てない
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = [local.instance_subnet_id]
    security_groups  = [local.splunk_sg_id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.splunk[0].arn
  }

  depends_on = [
    aws_iam_role_policy.splunk_execution,
    aws_iam_role_policy_attachment.splunk_execution,
    aws_iam_role_policy.splunk_task,
  ]
}

# ---------------------------------------------------------------- IAM
resource "aws_iam_role" "splunk_execution" {
  count = local.splunk_on_ecs ? 1 : 0

  name               = "${local.name_prefix}-splunk-exec"
  description        = "ECS task execution role of the Splunk task (ECR pull, CloudWatch Logs, admin password and HEC token from SSM)"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "splunk_execution" {
  count = local.splunk_on_ecs ? 1 : 0

  role       = aws_iam_role.splunk_execution[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# secrets の SecureString（AWS 管理の aws/ssm キーなので kms:Decrypt は要らない）
resource "aws_iam_role_policy" "splunk_execution" {
  count = local.splunk_on_ecs ? 1 : 0

  name = "${local.name_prefix}-splunk-exec"
  role = aws_iam_role.splunk_execution[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "Secrets"
      Effect   = "Allow"
      Action   = ["ssm:GetParameters"]
      Resource = [local.splunk_password_arn, local.splunk_token_parameter_arn]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "splunk_execution_perimeter" {
  count = local.splunk_on_ecs && local.perimeter_policy_arn != "" ? 1 : 0

  role       = aws_iam_role.splunk_execution[0].name
  policy_arn = local.perimeter_policy_arn
}

# アラートアクションのロール。できるのは土台のトピックへの publish だけ（トピックの鍵は AWS 管理の aws/sns なので kms の許可は要らない）
resource "aws_iam_role" "splunk_task" {
  count = local.splunk_on_ecs ? 1 : 0

  name               = "${local.name_prefix}-splunk-task"
  description        = "Splunk task - the netops_sns alert action publishes alerts to the SNS topic"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "splunk_task" {
  count = local.splunk_on_ecs ? 1 : 0

  name = "${local.name_prefix}-splunk-task"
  role = aws_iam_role.splunk_task[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "PublishAlerts"
      Effect   = "Allow"
      Action   = ["sns:Publish"]
      Resource = local.alerts_topic_arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "splunk_task_perimeter" {
  count = local.splunk_on_ecs && local.perimeter_policy_arn != "" ? 1 : 0

  role       = aws_iam_role.splunk_task[0].name
  policy_arn = local.perimeter_policy_arn
}
