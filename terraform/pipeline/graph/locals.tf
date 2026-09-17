# fukuda-nwc-poc - phase 2 graph root module. One Neptune cluster (IAM auth, one db.t4g.medium instance) holds the network topology
# (device vertices, link edges). The chat runtime and the web read it through boto3 neptunedata (Gremlin); the web can also edit it.
# Without this root module both fall back to the static data in agent/data/. Costs about 0.12 USD per hour while it exists - destroy it the same day.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / ロール名は terraform/base/core の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/core/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id          = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids      = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  runtime_sg_id   = data.terraform_remote_state.main.outputs.runtime_security_group_id
  web_sg_id       = data.terraform_remote_state.main.outputs.instance_security_group_id
  reader_role_ids = toset([data.terraform_remote_state.main.outputs.runtime_role_name, data.terraform_remote_state.main.outputs.web_role_name])
}
