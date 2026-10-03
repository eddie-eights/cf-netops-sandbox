# ---------------------------------------------------------------- Nautobot (ECS Fargate)
# 1 つのタスクに 3 つのコンテナ（同じタスクなので互いに localhost で届く）:
#   web     画面と API（8080）。上流の entrypoint が DB の migrate（post_upgrade）をしてから、nautobot/netops/bootstrap.py が
#           管理者・custom field・最初の seed（機器が 0 件のときだけ lab の定義から）・Job の有効化と JobHook を入れ、一度同期して、uwsgi を起こす
#   worker  Celery のワーカー。Job（nautobot/jobs/netops_jobs.py の「Telegraf と Neptune に同期」と、変更のたびに走る JobHook）を回す
#   redis   キャッシュと Celery のブローカー、同期のロック。中身は消えてよい
# 画面は Web の EC2 を踏み台にした SSM のポートフォワードで開く（outputs.tf のコマンド）。

resource "aws_cloudwatch_log_group" "nautobot" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "nautobot" {
  name = "${local.name_prefix}-nautobot"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_service_discovery_private_dns_namespace" "nautobot" {
  name        = local.service_namespace
  description = "Nautobot of ${local.name_prefix} (ECS)"
  vpc         = local.vpc_id
}

resource "aws_service_discovery_service" "nautobot" {
  name          = "nautobot"
  force_destroy = true

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.nautobot.id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 10
    }
  }
}

locals {
  # web と worker に同じものを渡す（どちらも同じ設定で DB と Redis につなぎ、Job は worker の中で AWS の API を呼ぶ）
  nautobot_environment = [
    { name = "NAUTOBOT_ALLOWED_HOSTS", value = "*" }, # 届くのは VPC の中（web の SG）からだけ
    { name = "NAUTOBOT_DB_HOST", value = aws_db_instance.nautobot.address },
    { name = "NAUTOBOT_DB_PORT", value = tostring(aws_db_instance.nautobot.port) },
    { name = "NAUTOBOT_DB_NAME", value = aws_db_instance.nautobot.db_name },
    { name = "NAUTOBOT_DB_USER", value = aws_db_instance.nautobot.username },
    { name = "NAUTOBOT_REDIS_HOST", value = "localhost" },
    { name = "NAUTOBOT_REDIS_PORT", value = "6379" },
    { name = "NAUTOBOT_SUPERUSER_NAME", value = var.admin_user },
    { name = "AWS_REGION", value = var.region },
    # Job の書き先（nautobot/netops/nb_sync.py）。空ならその片方を飛ばす
    { name = "NEPTUNE_ENDPOINT", value = local.neptune_endpoint },
    { name = "DIALIN_GNMI_PARAMETER", value = lookup(local.dialin_parameters, "gnmi-targets", "") },
    { name = "DIALIN_SNMP_PARAMETER", value = lookup(local.dialin_parameters, "snmp-agents", "") },
    { name = "TELEGRAF_CLUSTER", value = local.telegraf_cluster },
    { name = "TELEGRAF_DIALIN_SERVICE", value = local.telegraf_service },
  ]
  # SSM の SecureString（ops/up.sh が作る）。ECS のエージェントが実行ロールで読んでコンテナの環境変数にする
  nautobot_secrets = [
    { name = "NAUTOBOT_SECRET_KEY", valueFrom = local.secret_arns["secret-key"] },
    { name = "NAUTOBOT_DB_PASSWORD", valueFrom = local.secret_arns["db-password"] },
    { name = "NAUTOBOT_SUPERUSER_PASSWORD", valueFrom = local.secret_arns["admin-password"] },
  ]
  nautobot_log = { for c in ["web", "worker", "redis"] : c => {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.nautobot.name
      "awslogs-region"        = var.region
      "awslogs-stream-prefix" = c
    }
  } }
}

resource "aws_ecs_task_definition" "nautobot" {
  family                   = "${local.name_prefix}-nautobot"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "redis"
      image     = local.redis_image
      essential = true
      # ディスクに書かない（消えてよい。タスクが入れ替わればキャッシュもキューも空から）
      command          = ["redis-server", "--save", "", "--appendonly", "no"]
      logConfiguration = local.nautobot_log["redis"]
      healthCheck      = { command = ["CMD", "redis-cli", "ping"], interval = 10, timeout = 5, retries = 5, startPeriod = 10 }
    },
    {
      name      = "web"
      image     = local.image
      essential = true
      # entrypoint（上流のまま）が DB を待って migrate してから、この command が走る。bootstrap.py が落ちても画面は上げる（ログに理由が出る）
      command      = ["sh", "-c", "python /opt/nautobot/netops/bootstrap.py; exec nautobot-server start --ini /opt/nautobot/uwsgi.ini"]
      portMappings = [{ containerPort = 8080, protocol = "tcp" }]
      dependsOn    = [{ containerName = "redis", condition = "HEALTHY" }]
      # entrypoint が DB を待つ秒数（既定 30）。RDS とタスクを同時に作るので長めにする
      environment      = concat(local.nautobot_environment, [{ name = "MAX_DB_WAIT_TIME", value = "300" }])
      secrets          = local.nautobot_secrets
      logConfiguration = local.nautobot_log["web"]
      # 最初の起動は migrate に 5〜10 分かかる。startPeriod（最大 300 秒）のあいだの失敗は数えず、そのあと 60 秒 × 10 回まで待つ（合わせて 15 分）
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8080/health/', timeout=8)\" || exit 1"]
        interval    = 60
        timeout     = 10
        retries     = 10
        startPeriod = 300
      }
    },
    {
      name      = "worker"
      image     = local.image
      essential = true
      # migrate は web がする（NAUTOBOT_DOCKER_SKIP_INIT で entrypoint の post_upgrade を飛ばす）。web が上がってから起こす
      command          = ["nautobot-server", "celery", "worker", "--loglevel", "INFO", "--concurrency", "2"]
      dependsOn        = [{ containerName = "web", condition = "HEALTHY" }]
      environment      = concat(local.nautobot_environment, [{ name = "NAUTOBOT_DOCKER_SKIP_INIT", value = "true" }])
      secrets          = [for s in local.nautobot_secrets : s if s.name != "NAUTOBOT_SUPERUSER_PASSWORD"] # 管理者のパスワードは web の bootstrap だけが使う
      logConfiguration = local.nautobot_log["worker"]
    },
  ])

  lifecycle {
    precondition {
      condition     = try(data.terraform_remote_state.ecr.outputs.nautobot_repository_url, "") != "" && try(data.terraform_remote_state.ecr.outputs.redis_repository_url, "") != ""
      error_message = "terraform/base/ecr の state から nautobot_repository_url / redis_repository_url が読めない。terraform/base/ecr を先に apply する（ops/up.sh の手順 1）。"
    }
  }
}

resource "aws_ecs_service" "nautobot" {
  name            = "${local.name_prefix}-nautobot"
  cluster         = aws_ecs_cluster.nautobot.id
  task_definition = aws_ecs_task_definition.nautobot.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # 1 タスクだけ。入れ替えのときは古い方を止めてから新しい方を起こす（Redis がタスクの中にあるので、2 つ並ぶとロックもキューも別々になる）
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  enable_execute_command             = true # aws ecs execute-command で中に入る（nautobot-server nbshell など。outputs.tf）

  network_configuration {
    subnets          = [local.instance_subnet_id]
    security_groups  = [local.nautobot_sg_id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.nautobot.arn
  }

  depends_on = [
    aws_iam_role_policy_attachment.exec_managed,
    aws_iam_role_policy.exec_secrets,
    aws_iam_role_policy.task,
  ]
}

# Web（web/topology_view.py）がここを見て、トポロジの「Neptune で編集」を止める（Nautobot が正のあいだ、物理層は Nautobot で編集する）
resource "aws_ssm_parameter" "url" {
  name        = "/${local.name_prefix}/nautobot/url"
  description = "URL of Nautobot inside the VPC. While it exists the web UI sends topology edits to Nautobot."
  type        = "String"
  value       = local.url
}
