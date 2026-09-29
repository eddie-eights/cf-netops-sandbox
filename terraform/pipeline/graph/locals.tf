# netops-poc - PIPELINE graph root module. One Neptune cluster (IAM auth, one db.t4g.medium instance) holds the network topology
# (device vertices, link edges). The chat runtime and the web read it through boto3 neptunedata (Gremlin); the web can also edit it.
# Without this root module both fall back to the static data in agent/data/. Costs about 0.12 USD per hour while it exists - destroy it the same day.

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / ロール名は terraform/base/core の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/core/terraform.tfstate"
  }

  # SG の ID（security_group_ids）は 2026-09-29 から。それより前の state（全部で共有する internal 1 つ）なら apply の前に止める。
  # destroy ではこの条件を見ないので、locals の SG の try と合わせて古い state のまま ops/down.sh で消せる（Terraform 1.16 で確認）
  lifecycle {
    postcondition {
      condition     = can(self.outputs.security_group_ids)
      error_message = "terraform/base/core の state に security_group_ids が無い（2026-09-29 より前の SG）。先に ops/down.sh で消してから ops/up.sh を打ち直す"
    }
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id     = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  # SG は古い state の destroy でも評価できるように try（空のまま apply に進まないよう remote_state の postcondition で止める）
  neptune_sg_id   = try(data.terraform_remote_state.main.outputs.security_group_ids["neptune"], "")
  lambda_sg_id    = try(data.terraform_remote_state.main.outputs.security_group_ids["lambda"], "") # sync.tf の status Lambda
  reader_role_ids = toset([data.terraform_remote_state.main.outputs.runtime_role_name, data.terraform_remote_state.main.outputs.web_role_name])
}
