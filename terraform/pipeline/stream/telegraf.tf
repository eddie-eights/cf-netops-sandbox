# ---------------------------------------------------------------- Telegraf (ECS on Fargate + internal NLB)
# 機器の SNMP のポーリング・gNMI の購読・trap・syslog・MDT を受けて MSK に書く Telegraf を、Fargate で動かす（2026-09-28 まで terraform/pipeline/lab の EC2）。
# 同じイメージのタスクを役割（telegraf/telegraf.sh の TELEGRAF_ROLE）で 2 つのサービスに分ける（2026-10-04 ユーザー決定。名前は dialout / dialin にそろえる）:
#   telegraf-dialout  機器から送ってくる trap・syslog・MDT を NLB の後ろで受ける。機器の一覧を持たず、台数を増やしても同じものを 2 回書かない
#   telegraf-dialin   gNMI の購読と SNMP のポーリングでこちらから取りにいく（lab の gNMI の変換もここ）。機器の一覧を持ち、
#                     2 つ立てると同じ機器から 2 回取って MSK に 2 回書くので 1 つ。NLB には付けない
# dialin の機器の一覧と認証情報は SSM パラメータから ECS の secrets で渡す（タスクを起こすときに読むので、変えたらサービスを作り直す）:
#   /<接頭辞>/telegraf-dialin/<出どころ>/gnmi-targets・snmp-agents   String。出どころは lab（var.gnmi_targets / var.snmp_agents。Terraform が書く）か
#                     nautobot（var.dialin_targets_from_nautobot。最初の値だけ Terraform が書き、あとは terraform/pipeline/nautobot の Job が書き換えて
#                     サービスを作り直す。Terraform は値の変化を見ない）
#   /<接頭辞>/telegraf-dialin/gnmi-username・gnmi-password・snmp-community   SecureString。ops/up.sh が作る（値を state に入れない）
# イメージは telegraf/Dockerfile（公式の telegraf に設定のテンプレートと入口を足したもの）で、ops/up.sh が ECR の <接頭辞>-telegraf に置く。
# lab の管理ネットワーク（203.0.113.0/24）は lab の EC2 の中の docker network なので、terraform/pipeline/lab（forward_to_telegraf）が VPC のルートと
# lab.sh forward で届ける:
#   ポーリング  telegraf-dialin → 機器の SNMP（161/udp）と gNMI（57400/tcp）。送り元はタスクの IP で、作り直すたびに変わるので、lab.sh forward は
#               タスクのサブネットの CIDR（SSM の /<接頭辞>/telegraf-source-cidr）で通す
#   trap        機器 → lab の EC2 の 162/udp → DNAT → 下の NLB の 162 → タスクの 1162（非 root は 1024 未満で待てない）
#   syslog      機器 → lab の EC2 の 5140/udp → DNAT → NLB の 5140 → タスクの 5140
#   MDT         本番の Cisco → NLB の 57000/tcp → タスクの 57000（dial-out。lab の SR Linux は送れないので lab からは来ない。docs/collection.md）
# タスクの IP は作り直すと変わるので、DNAT の宛先は変わらない NLB の IP にする（SSM の /<接頭辞>/telegraf-address）。
# NLB は UDP の送り元の IP を残す（UDP のターゲットは client IP preservation が既定で、Spark とエージェントは送り元の IP で機器を引く）。
# MDT（TCP）は残さない（IP のターゲットの既定）。機器は MDT の中で node_id を名乗るので、送り元の IP は要らない。
# SG は NLB（telegraf_dialout_nlb）と 2 つのタスク（telegraf_dialout / telegraf_dialin）で別々で、ルールは terraform/base/core の security_groups.tf の通信の表にある:
#   telegraf_dialout_nlb  管理ネットワークの CIDR から udp 162 / 5140 を、mdt_source_cidrs（土台の変数。既定は空）から tcp 57000 を受け（送り元が機器の管理 IP のまま）、
#                         telegraf_dialout の SG へ udp 1162 / 5140、tcp 57000 と tcp 8080（ヘルスチェック）を送る
#   telegraf_dialout      NLB の SG から受け（送り元の IP が残っても、NLB の SG を参照したルールで通る）、MSK の 9098・エンドポイントと S3 の 443 へ送る
#   telegraf_dialin       何も受けない。管理ネットワークの udp 161 / tcp 57400・MSK の 9098・エンドポイントと S3 の 443 へ送る

locals {
  telegraf_repository_url = try(data.terraform_remote_state.ecr.outputs.telegraf_repository_url, "")
  telegraf_image          = "${local.telegraf_repository_url}:${var.telegraf_image_tag}"
  telegraf_log_group      = "/ecs/${local.name_prefix}-telegraf"

  # dialin の SSM パラメータ（上の注記）。機器の一覧は出どころでパスを分ける（切り替えると Terraform が古い方を消し、タスクの参照先も変わる）
  dialin_parameter_prefix = "/${local.name_prefix}/telegraf-dialin"
  dialin_target_source    = var.dialin_targets_from_nautobot ? "nautobot" : "lab"
  dialin_targets          = { "gnmi-targets" = var.gnmi_targets, "snmp-agents" = var.snmp_agents }
  dialin_target_names     = { for k, _ in local.dialin_targets : k => "${local.dialin_parameter_prefix}/${local.dialin_target_source}/${k}" }
  # ops/up.sh の ensure_fixed_secret が作る SecureString（タスクの環境変数名 → パラメータの名前の最後）
  dialin_credentials = { GNMI_USERNAME = "gnmi-username", GNMI_PASSWORD = "gnmi-password", SNMP_COMMUNITY = "snmp-community" }
  ssm_parameter_arn  = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter"

  # NLB の受け口 → タスクのポート（telegraf/telegraf.conf.in の inputs.snmp_trap、inputs.syslog と inputs.cisco_telemetry_mdt）
  telegraf_ports = {
    trap   = { listener = 162, container = 1162, protocol = "UDP" }
    syslog = { listener = 5140, container = 5140, protocol = "UDP" }
    mdt    = { listener = 57000, container = 57000, protocol = "TCP" }
  }
}

data "aws_subnet" "telegraf" {
  id = local.telegraf_subnet_id
}

# ---------------------------------------------------------------- NLB
resource "aws_lb" "telegraf_dialout" {
  name               = "${local.name_prefix}-tg"
  internal           = true
  load_balancer_type = "network"
  subnets            = [local.telegraf_subnet_id]
  # NLB の SG は作るときにしか付けられない（後から足すと作り直し。付けて作った NLB なら入れ替えはできる）
  security_groups = [local.telegraf_dialout_nlb_sg_id]

  # 名前は 32 文字までなので -tg のまま（接頭辞が 22 文字まで）
  tags = { Name = "${local.name_prefix}-telegraf-dialout" }
}

# NLB のアドレス（サブネット 1 つなので ENI も 1 つ）。lab.sh forward の DNAT の宛先
data "aws_network_interface" "telegraf_dialout_lb" {
  filter {
    name   = "description"
    values = ["ELB ${aws_lb.telegraf_dialout.arn_suffix}"]
  }
  filter {
    name   = "vpc-id"
    values = [local.vpc_id]
  }
}

resource "aws_lb_target_group" "telegraf_dialout" {
  for_each = local.telegraf_ports

  name        = "${local.name_prefix}-${each.key}"
  port        = each.value.container
  protocol    = each.value.protocol
  target_type = "ip"
  vpc_id      = local.vpc_id

  # UDP は送り元（機器の管理 IP）を残す。TCP（MDT）は残さない（IP のターゲットの既定。機器は node_id を名乗る）
  preserve_client_ip = each.value.protocol == "UDP"
  # タスクを作り直すとき、古いタスクを長く待たない
  deregistration_delay = 10

  # UDP は応答で生死を見られないので、Telegraf の outputs.health（telegraf.conf.in）を見る。TCP（MDT）も同じものを見る
  health_check {
    protocol            = "HTTP"
    port                = "8080"
    path                = "/"
    interval            = 10
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = { Name = "${local.name_prefix}-telegraf-dialout-${each.key}" }
}

resource "aws_lb_listener" "telegraf_dialout" {
  for_each = local.telegraf_ports

  load_balancer_arn = aws_lb.telegraf_dialout.arn
  port              = each.value.listener
  protocol          = each.value.protocol

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.telegraf_dialout[each.key].arn
  }
}

resource "aws_ssm_parameter" "telegraf_address" {
  name        = "/${local.name_prefix}/telegraf-address"
  type        = "String"
  value       = data.aws_network_interface.telegraf_dialout_lb.private_ip
  description = "Private IP of the Telegraf dial-out NLB. Read by lab.sh forward on the lab EC2 (trap / syslog DNAT target)."
}

resource "aws_ssm_parameter" "telegraf_source_cidr" {
  name        = "/${local.name_prefix}/telegraf-source-cidr"
  type        = "String"
  value       = data.aws_subnet.telegraf.cidr_block
  description = "CIDR of the subnet of the Telegraf dial-in task (its IP changes on every replacement). Read by lab.sh forward on the lab EC2 (SNMP / gNMI polling source)."
}

# dialin の機器の一覧（lab から）。Terraform が値を持つ
resource "aws_ssm_parameter" "dialin_targets_lab" {
  for_each = var.dialin_targets_from_nautobot ? {} : local.dialin_targets

  name        = local.dialin_target_names[each.key]
  type        = "String"
  value       = each.value
  description = "${each.key} of the Telegraf dial-in task, from the lab definition (python3 lab/lab_topology.py lab --${each.key}). ECS secrets of the task."
}

# dialin の機器の一覧（Nautobot から）。最初の値は lab から（Nautobot の最初の seed も lab なので同じ）で、あとは terraform/pipeline/nautobot の Job
# （nautobot/jobs の dialin の同期）が書き換えてサービスを作り直す。Terraform は値の変化を見ない
resource "aws_ssm_parameter" "dialin_targets_nautobot" {
  for_each = var.dialin_targets_from_nautobot ? local.dialin_targets : {}

  name        = local.dialin_target_names[each.key]
  type        = "String"
  value       = each.value
  description = "${each.key} of the Telegraf dial-in task, written by the Nautobot job (terraform/pipeline/nautobot). ECS secrets of the task."

  lifecycle {
    ignore_changes = [value]
  }
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

# ECS Exec（tg test / tg gnmi）を使うので readonlyRootFilesystem は付けない（ECS Exec が対応していない）。telegraf.sh が書くのは /tmp だけ。
# どちらのタスクも同じイメージ・同じロール・同じロググループで、ログのストリームの頭（dialout / dialin）で見分ける
resource "aws_ecs_task_definition" "telegraf_dialout" {
  family                   = "${local.name_prefix}-telegraf-dialout"
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

  container_definitions = jsonencode([
    {
      name      = "telegraf"
      image     = local.telegraf_image
      essential = true
      portMappings = [
        { containerPort = 1162, protocol = "udp" },  # trap（NLB の 162 から）
        { containerPort = 5140, protocol = "udp" },  # syslog
        { containerPort = 57000, protocol = "tcp" }, # MDT の dial-out
        { containerPort = 8080, protocol = "tcp" },  # outputs.health（NLB のヘルスチェック）
      ]
      environment = [
        { name = "TELEGRAF_ROLE", value = "dialout" },
        { name = "AWS_REGION", value = var.region },
        { name = "KAFKA_BROKERS", value = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam },
        { name = "SYSLOG_STANDARD", value = var.syslog_standard },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.telegraf.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "dialout"
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

resource "aws_ecs_task_definition" "telegraf_dialin" {
  family                   = "${local.name_prefix}-telegraf-dialin"
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

  # 受け口は無い（portMappings なし。outputs.health の 8080 は開くが、見る NLB は無い）
  container_definitions = jsonencode([
    {
      name      = "telegraf"
      image     = local.telegraf_image
      essential = true
      environment = [
        { name = "TELEGRAF_ROLE", value = "dialin" },
        { name = "AWS_REGION", value = var.region },
        { name = "KAFKA_BROKERS", value = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam },
        { name = "SNMP_POLL", value = var.snmp_poll ? "1" : "0" },
      ]
      # 機器の一覧（String）と認証情報（SecureString）。telegraf.conf.in は ${GNMI_USERNAME} などで読み、SNMP_AGENTS / GNMI_TARGETS は telegraf.sh が埋める
      secrets = concat(
        [
          { name = "GNMI_TARGETS", valueFrom = "${local.ssm_parameter_arn}${local.dialin_target_names["gnmi-targets"]}" },
          { name = "SNMP_AGENTS", valueFrom = "${local.ssm_parameter_arn}${local.dialin_target_names["snmp-agents"]}" },
        ],
        [for env, leaf in local.dialin_credentials : { name = env, valueFrom = "${local.ssm_parameter_arn}${local.dialin_parameter_prefix}/${leaf}" }],
      )
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.telegraf.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "dialin"
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

# 受ける側（dialout）。NLB の後ろ
resource "aws_ecs_service" "telegraf_dialout" {
  name            = "${local.name_prefix}-telegraf-dialout"
  cluster         = aws_ecs_cluster.telegraf.id
  task_definition = aws_ecs_task_definition.telegraf_dialout.arn
  # 機器から送ってくるものだけなので、増やしても同じものを 2 回書かない（NLB が振り分ける）。PoC は 1 つ
  desired_count = 1
  launch_type   = "FARGATE"

  # aws ecs execute-command でタスクの中に入れる（設定を見る程度。tg test / tg gnmi は telegraf-dialin で打つ）
  enable_execute_command = true

  # 新しいタスクが NLB のヘルスチェックを通ってから古いタスクを外す（入れ替えのあいだも trap と syslog を落とさない）
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = [local.telegraf_subnet_id]
    security_groups  = [local.telegraf_dialout_sg_id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = local.telegraf_ports
    content {
      target_group_arn = aws_lb_target_group.telegraf_dialout[load_balancer.key].arn
      container_name   = "telegraf"
      container_port   = load_balancer.value.container
    }
  }

  depends_on = [
    aws_lb_listener.telegraf_dialout,
    aws_iam_role_policy.telegraf_task,
    aws_iam_role_policy_attachment.telegraf_execution,
  ]
}

# 取りにいく側（dialin）。NLB に付けない。terraform/pipeline/nautobot の Job が機器の一覧を書き換えたあと、このサービスを作り直す（force-new-deployment）
resource "aws_ecs_service" "telegraf_dialin" {
  name            = "${local.name_prefix}-telegraf-dialin"
  cluster         = aws_ecs_cluster.telegraf.id
  task_definition = aws_ecs_task_definition.telegraf_dialin.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # aws ecs execute-command でタスクの中に入れる（tg test / tg gnmi。コマンドは output telegraf_exec_command）
  enable_execute_command = true

  # 2 つ同時に立てない（同じ機器を 2 回ポーリング・購読して MSK に 2 回書かない）。入れ替えでは古いタスクを止めてから新しいタスクを起こす。
  # gNMI は lab（SR Linux は MDT を送れない）のためのもので、本番の Cisco は MDT の dial-out で送らせる方針（docs/collection.md）
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = [local.telegraf_subnet_id]
    security_groups  = [local.telegraf_dialin_sg_id]
    assign_public_ip = false
  }

  depends_on = [
    aws_iam_role_policy.telegraf_task,
    aws_iam_role_policy.telegraf_execution,
    aws_iam_role_policy_attachment.telegraf_execution,
    aws_ssm_parameter.dialin_targets_lab,
    aws_ssm_parameter.dialin_targets_nautobot,
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
  description        = "ECS task execution role of the Telegraf tasks (ECR pull, CloudWatch Logs, dial-in targets and credentials from SSM)"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "telegraf_execution" {
  role       = aws_iam_role.telegraf_execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# dialin の secrets（機器の一覧と SecureString の認証情報。AWS 管理の aws/ssm キーなので kms:Decrypt は要らない）
resource "aws_iam_role_policy" "telegraf_execution" {
  name = "${local.name_prefix}-telegraf-exec"
  role = aws_iam_role.telegraf_execution.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "DialinParameters"
      Effect   = "Allow"
      Action   = ["ssm:GetParameters"]
      Resource = "${local.ssm_parameter_arn}${local.dialin_parameter_prefix}/*"
    }]
  })
}

resource "aws_iam_role" "telegraf_task" {
  name               = "${local.name_prefix}-telegraf-task"
  description        = "Telegraf task - write SNMP / gNMI / trap / syslog / MDT to MSK (IAM auth), ECS Exec"
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
