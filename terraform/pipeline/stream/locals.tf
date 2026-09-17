# fukuda-nwc-poc - phase 2 stream root module. MSK (2 brokers, IAM auth) receives SNMP polls and traps from Telegraf on the lab EC2 (terraform/pipeline/lab),
# the Spark job of terraform/pipeline/analytics writes link_down / trap anomalies to the DynamoDB table made here, and MSK Connect keeps the raw
# messages in the asset bucket (S3 sink). The chat runtime, the web and the workflow tools read the anomaly table. Costs about 0.13 USD per hour while it exists - destroy it the same day.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / バケット / ロール名は terraform/base/core と terraform/pipeline/lab の state から読む
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
  route_table_ids   = data.terraform_remote_state.main.outputs.route_table_ids
  endpoint_sg_id    = data.terraform_remote_state.main.outputs.endpoint_security_group_id
  bucket            = data.terraform_remote_state.main.outputs.kb_bucket_name
  reader_role_names = toset([data.terraform_remote_state.main.outputs.runtime_role_name, data.terraform_remote_state.main.outputs.web_role_name])

  # lab が無いと Telegraf の送信元が無い。下の precondition で「lab を先に」と出す
  lab_sg_id     = try(data.terraform_remote_state.lab.outputs.lab_security_group_id, "")
  lab_role_name = try(data.terraform_remote_state.lab.outputs.lab_role_name, "")

  # ブローカー 2 台 = サブネット 2 つ（terraform/base/core の Runtime サブネット）
  broker_subnet_ids = slice(local.subnet_ids, 0, 2)

  # arn:aws:kafka:<region>:<account>:cluster/<name>/<uuid> → topic/<name>/<uuid>/* と group/<name>/<uuid>/*
  topic_arns = "${replace(aws_msk_cluster.stream.arn, ":cluster/", ":topic/")}/*"
  group_arns = "${replace(aws_msk_cluster.stream.arn, ":cluster/", ":group/")}/*"

  bucket_arn = "arn:${local.partition}:s3:::${local.bucket}"
}
