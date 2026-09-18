# netops-poc - optional lab root module. One EC2 (Amazon Linux 2023 arm64) runs Docker + containerlab with the wanlab topology
# (6 FRR routers with BGP, 4 snmpd sidecars, 4 hosts, all fictional addresses). Reached with SSM Session Manager.
# Images come from ECR (terraform/base/ecr), the containerlab rpm and configs from the S3 bucket of terraform/base/core. Stop the instance when not in use.
# If the Telegraf rpm is also in lab/, Telegraf polls the CE routers over SNMP, receives their traps and writes to MSK (terraform/pipeline/stream).

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_ssm_parameter" "al2023" {
  name = var.ami_ssm_parameter
}

# VPC / subnet / endpoint SG / bucket は terraform/base/core の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/core/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id         = data.terraform_remote_state.main.outputs.vpc_id
  subnet_id      = data.terraform_remote_state.main.outputs.instance_subnet_id
  endpoint_sg_id = data.terraform_remote_state.main.outputs.endpoint_security_group_id
  bucket         = data.terraform_remote_state.main.outputs.kb_bucket_name
}
