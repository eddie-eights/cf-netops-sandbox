# netops-poc - PIPELINE stream root module. MSK (2 brokers, IAM auth) receives SNMP polls, traps and FRR logs from the Telegraf EC2 (terraform/pipeline/lab, create_telegraf),
# and the Spark job of terraform/pipeline/analytics reads them (raw messages go to S3 Tables, anomalies to Neptune and to the S3 Tables anomaly_events,
# not here since 2026-09-24). The MSK Connect S3 sink that also copied the raw messages to the asset bucket was removed on 2026-09-26
# (Spark already stores every topic in S3 Tables). Costs about 0.57 USD per hour while it exists - destroy it the same day.

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / ロール名は terraform/base/core と terraform/pipeline/lab の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/core/terraform.tfstate"
  }
}

data "terraform_remote_state" "lab" {
  backend = "local"

  config = {
    path = "${path.module}/../lab/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id            = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids        = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  reader_role_names = toset([data.terraform_remote_state.main.outputs.runtime_role_name, data.terraform_remote_state.main.outputs.web_role_name])

  # Telegraf の EC2（terraform/pipeline/lab の create_telegraf）が無いと送信元が無い。network.tf の precondition で「lab を先に」と出す
  telegraf_sg_id     = try(data.terraform_remote_state.lab.outputs.telegraf_security_group_id, "")
  telegraf_role_name = try(data.terraform_remote_state.lab.outputs.telegraf_role_name, "")

  # ブローカー 2 台 = サブネット 2 つ（terraform/base/core の Runtime サブネット）
  broker_subnet_ids = slice(local.subnet_ids, 0, 2)

  # arn:aws:kafka:<region>:<account>:cluster/<name>/<uuid> → topic/<name>/<uuid>/*
  topic_arns = "${replace(aws_msk_cluster.stream.arn, ":cluster/", ":topic/")}/*"
}
