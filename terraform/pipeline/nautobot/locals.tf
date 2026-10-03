# nwc-poc - PIPELINE nautobot root module. Nautobot (source of truth of the devices and cables) on ECS Fargate with PostgreSQL on RDS.
# One task runs three containers: web (UI / API on 8080), a Celery worker (runs the jobs) and Redis (cache and Celery broker).
# The NetOps jobs of the image (nautobot/jobs) read the devices and cables of Nautobot and bring two things in line with them:
#   1. the targets of the Telegraf dial-in task - SSM parameters of terraform/pipeline/stream, then a new deployment of the dial-in service
#   2. the physical layer of the topology in Neptune (terraform/pipeline/graph), written with Gremlin (agent/graph.py sync_physical)
# A job hook runs the job on every change of a device / interface / cable / IP address / service, and the task runs it once at start.
# The first start seeds Nautobot from the lab definition (lab_seed.json in the image), so the lab works without typing anything.
# Costs about 0.13 USD per hour (Fargate ARM 2 vCPU / 4 GB + RDS db.t4g.micro) - ops/down.sh destroys this root, and what was edited in Nautobot goes with it.

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG は terraform/base/core の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/core/terraform.tfstate"
  }

  # Nautobot の SG（nautobot / nautobot_db）は 2026-10-04 から。それより前の state なら apply の前に止める。
  # destroy ではこの条件を見ないので、locals の SG の try と合わせて古い state のまま ops/down.sh で消せる
  lifecycle {
    postcondition {
      condition     = can(self.outputs.security_group_ids["nautobot_db"])
      error_message = "terraform/base/core の state に Nautobot の SG（nautobot / nautobot_db）が無い（2026-10-04 より前の土台）。先に terraform/base/core を apply する（ops/up.sh の手順 3）"
    }
  }
}

# Nautobot と Redis のイメージは terraform/base/ecr（ops/up.sh の手順 1 と 2）
data "terraform_remote_state" "ecr" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/ecr/terraform.tfstate"
  }
}

# Telegraf の dialin の機器の一覧（SSM）とサービスは terraform/pipeline/stream。無ければ Job は一覧を触らない
data "terraform_remote_state" "stream" {
  backend = "local"

  config = {
    path = "${path.module}/../stream/terraform.tfstate"
  }
}

# Neptune は terraform/pipeline/graph。無ければ Job は Neptune を触らない
data "terraform_remote_state" "graph" {
  backend = "local"

  config = {
    path = "${path.module}/../graph/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id     = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  # ECS のタスクを置くサブネット（web の EC2 と同じ。SSM のポートフォワードは web の EC2 から届く）
  instance_subnet_id = data.terraform_remote_state.main.outputs.instance_subnet_id
  # SSM のポートフォワードの踏み台（Nautobot の UI。outputs.tf のコマンド）
  web_instance_id = try(data.terraform_remote_state.main.outputs.web_instance_id, "")
  # SG は古い state の destroy でも評価できるように try（空のまま apply に進まないよう remote_state の postcondition で止める）
  nautobot_sg_id    = try(data.terraform_remote_state.main.outputs.security_group_ids["nautobot"], "")
  nautobot_db_sg_id = try(data.terraform_remote_state.main.outputs.security_group_ids["nautobot_db"], "")
  # terraform/base/core の perimeter.tf の Deny（VPC エンドポイントを通らない AWS の API を拒む）。NETWORK_PERIMETER=0 なら空
  perimeter_policy_arn = try(data.terraform_remote_state.main.outputs.network_perimeter_policy_arn, "")

  image       = "${try(data.terraform_remote_state.ecr.outputs.nautobot_repository_url, "")}:${var.nautobot_image_tag}"
  redis_image = "${try(data.terraform_remote_state.ecr.outputs.redis_repository_url, "")}:${var.redis_image_tag}"
  log_group   = "/ecs/${local.name_prefix}-nautobot"
  # analytics の名前空間（<接頭辞>.internal）とは別にする（このルートは analytics が無くても作れる）
  service_namespace = "${local.name_prefix}-nautobot.internal"
  url               = "http://nautobot.${local.service_namespace}:8080"

  # stream の dialin（Nautobot が一覧を持つ形 = dialin_targets_from_nautobot で apply されているときだけ）。違えば空で、Job は一覧を触らない
  dialin_from_nautobot = try(data.terraform_remote_state.stream.outputs.telegraf_dialin_targets_source, "") == "nautobot"
  dialin_parameters    = local.dialin_from_nautobot ? data.terraform_remote_state.stream.outputs.telegraf_dialin_target_parameters : {}
  telegraf_cluster     = local.dialin_from_nautobot ? data.terraform_remote_state.stream.outputs.telegraf_cluster_name : ""
  telegraf_service     = local.dialin_from_nautobot ? data.terraform_remote_state.stream.outputs.telegraf_dialin_service_name : ""

  # Neptune（graph が無ければ空）。agent/graph.py は <host>:8182 の形で受ける
  neptune_host        = try(data.terraform_remote_state.graph.outputs.cluster_endpoint, "")
  neptune_endpoint    = local.neptune_host != "" ? "${local.neptune_host}:8182" : ""
  neptune_resource_id = try(data.terraform_remote_state.graph.outputs.cluster_resource_id, "")

  # ops/up.sh が無ければ乱数で作る SecureString（値は Terraform の state に載せない。ops/down.sh が消す）
  secret_parameters = {
    secret-key     = "/${local.name_prefix}/nautobot/secret-key"     # Django の SECRET_KEY
    admin-password = "/${local.name_prefix}/nautobot/admin-password" # 画面の管理者（nautobot/netops/bootstrap.py が作る）
    db-password    = "/${local.name_prefix}/nautobot/db-password"    # RDS のマスターユーザー
  }
  secret_arns = { for k, v in local.secret_parameters : k => "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${v}" }
}
