# nwc-poc - PIPELINE stream root module. MSK (2 brokers, IAM auth) receives SNMP polls, gNMI state, traps and syslog from Telegraf (telegraf.tf: two ECS on Fargate tasks - a receiver
# behind an internal NLB and a poller, split on 2026-10-04; it was an EC2 of terraform/pipeline/lab until 2026-09-28),
# and the Spark job of terraform/pipeline/analytics reads them (raw messages go to S3 Tables, Prometheus, OpenSearch and Splunk; detection is done by Grafana and Splunk since 2026-10-02). The MSK Connect S3 sink that also copied the raw messages to the asset bucket was removed on 2026-09-26
# (Spark already stores every topic in S3 Tables). Costs about 0.61 USD per hour while it exists - destroy it the same day.

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / ロール名は terraform/base/core、Telegraf のイメージのリポジトリは terraform/base/ecr の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/core/terraform.tfstate"
  }

  # SG の ID（security_group_ids）は 2026-09-29 から、Telegraf の SG のキーが dialout / dialin になったのは 2026-10-04 から。
  # それより前の state なら apply の前に止める。destroy ではこの条件を見ないので、locals の SG の try と合わせて古い state のまま ops/down.sh で消せる（Terraform 1.16 で確認）
  lifecycle {
    postcondition {
      condition     = can(self.outputs.security_group_ids["telegraf_dialin"])
      error_message = "terraform/base/core の state に telegraf_dialin の SG が無い（2026-10-04 より前の SG）。先に ops/down.sh で消してから ops/up.sh を打ち直す"
    }
  }
}

data "terraform_remote_state" "ecr" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/ecr/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id     = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  # SG は古い state の destroy でも評価できるように try（空のまま apply に進まないよう remote_state の postcondition で止める）
  telegraf_dialout_sg_id     = try(data.terraform_remote_state.main.outputs.security_group_ids["telegraf_dialout"], "")
  telegraf_dialin_sg_id      = try(data.terraform_remote_state.main.outputs.security_group_ids["telegraf_dialin"], "")
  telegraf_dialout_nlb_sg_id = try(data.terraform_remote_state.main.outputs.security_group_ids["telegraf_dialout_nlb"], "")
  msk_sg_id                  = try(data.terraform_remote_state.main.outputs.security_group_ids["msk"], "")
  reader_role_names          = toset([data.terraform_remote_state.main.outputs.runtime_role_name, data.terraform_remote_state.main.outputs.web_role_name])

  # Telegraf のタスク（2 つとも）と NLB はサブネット a（lab の EC2 と Web の EC2 と同じ）
  telegraf_subnet_id = data.terraform_remote_state.main.outputs.instance_subnet_id
  # terraform/base/core の perimeter.tf の Deny（VPC エンドポイントを通らない AWS の API を拒む）。NETWORK_PERIMETER=0 か古い state なら空
  perimeter_policy_arn = try(data.terraform_remote_state.main.outputs.network_perimeter_policy_arn, "")

  # ブローカー 2 台 = サブネット 2 つ（terraform/base/core の Runtime サブネット）
  broker_subnet_ids = slice(local.subnet_ids, 0, 2)

  # arn:aws:kafka:<region>:<account>:cluster/<name>/<uuid> → topic/<name>/<uuid>/*
  topic_arns = "${replace(aws_msk_cluster.stream.arn, ":cluster/", ":topic/")}/*"
}
