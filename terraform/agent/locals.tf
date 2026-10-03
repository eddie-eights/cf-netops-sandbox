# nwc-poc - agent root module (feature "agent"). The AgentCore Runtime (VPC mode) that answers the chat, its
# execution policy and the guardrail (the runtime reaches Bedrock / SSM / the gateway through the interface endpoints of
# terraform/base/core, which ops/up.sh asks for when AGENT=1; the perimeter there denies calls from outside the VPC). Optionally (create_knowledge_base = true) a Bedrock
# Knowledge Base on OpenSearch Serverless for RAG - off by default because the collection costs about 0.33 USD per hour.
# The VPC, the security groups, the S3 bucket, the chat web EC2 and the runtime IAM role come from terraform/base/core
# (read through terraform_remote_state), so this root can be created and destroyed on its own while the base stays.

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / バケット / ロールは terraform/base/core の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../base/core/terraform.tfstate"
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

# エージェントのイメージの置き場は terraform/base/ecr の state から読む
data "terraform_remote_state" "ecr" {
  count   = var.agent_image_uri == "" ? 1 : 0
  backend = "local"

  config = {
    path = "${path.module}/../base/ecr/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id     = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  # SG は古い state の destroy でも評価できるように try（空のまま apply に進まないよう remote_state の postcondition で止める）
  runtime_sg_id     = try(data.terraform_remote_state.main.outputs.security_group_ids["runtime"], "")
  lambda_sg_id      = try(data.terraform_remote_state.main.outputs.security_group_ids["lambda"], "") # kb.tf の索引を作る Lambda
  runtime_role_name = data.terraform_remote_state.main.outputs.runtime_role_name
  runtime_role_arn  = data.terraform_remote_state.main.outputs.runtime_role_arn
  web_role_name     = data.terraform_remote_state.main.outputs.web_role_name
  bucket_name       = data.terraform_remote_state.main.outputs.kb_bucket_name
  bucket_arn        = data.terraform_remote_state.main.outputs.kb_bucket_arn
  # 土台の OpenSearch Serverless の VPC エンドポイント（create_opensearch_endpoint=true のときだけある。古い state には output が無い）
  aoss_vpce_id = try(data.terraform_remote_state.main.outputs.opensearch_vpc_endpoint_id, "")
  # terraform/base/core の perimeter.tf（NETWORK_PERIMETER=0 か古い state なら空）
  perimeter_policy_arn        = try(data.terraform_remote_state.main.outputs.network_perimeter_policy_arn, "")
  perimeter_exempt_principals = try(data.terraform_remote_state.main.outputs.perimeter_exempt_principals, [])

  kb = var.create_knowledge_base

  rerank           = var.rerank_model_id != ""
  rerank_model_arn = local.rerank ? "arn:${local.partition}:bedrock:${var.region}::foundation-model/${var.rerank_model_id}" : ""

  agent_image_uri = var.agent_image_uri != "" ? var.agent_image_uri : "${data.terraform_remote_state.ecr[0].outputs.agent_repository_url}:${var.agent_image_tag}"

  collection_name = "${local.name_prefix}-kb"
  index_name      = "kb-index"

  param_prefix = "/${local.name_prefix}"

  # AgentCore Runtime の名前にはハイフンが使えないので、接頭辞の - を _ にして _agent を付ける（<owner>-nwc-poc -> <owner>_nwc_poc_agent）。
  # ops/down.sh も同じ規則でロググループ（/aws/bedrock-agentcore/runtimes/<この名前>-*）を探すので、変えるなら両方を合わせる
  runtime_name = var.runtime_name != "" ? var.runtime_name : "${replace(local.name_prefix, "-", "_")}_agent"
}
