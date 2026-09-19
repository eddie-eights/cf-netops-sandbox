# netops-poc - agent root module (feature "agent"). The AgentCore Runtime (VPC mode) that answers the chat, its
# execution policy, the guardrail, the interface endpoints the runtime needs (ecr / logs / bedrock-runtime, 2 AZ) and the
# bedrock-agentcore endpoint the chat web invokes it through. Optionally (create_knowledge_base = true) a Bedrock
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

# assumed-role のセッションなら元のロールの ARN、IAM ユーザーならそのままユーザーの ARN になる
data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

# VPC / サブネット / SG / バケット / ロールは terraform/base/core の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../base/core/terraform.tfstate"
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

  vpc_id            = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids        = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  instance_subnet   = data.terraform_remote_state.main.outputs.instance_subnet_id
  endpoint_sg_id    = data.terraform_remote_state.main.outputs.endpoint_security_group_id
  runtime_sg_id     = data.terraform_remote_state.main.outputs.runtime_security_group_id
  runtime_role_name = data.terraform_remote_state.main.outputs.runtime_role_name
  runtime_role_arn  = data.terraform_remote_state.main.outputs.runtime_role_arn
  web_role_name     = data.terraform_remote_state.main.outputs.web_role_name
  bucket_name       = data.terraform_remote_state.main.outputs.kb_bucket_name
  bucket_arn        = data.terraform_remote_state.main.outputs.kb_bucket_arn

  kb = var.create_knowledge_base

  kb_admin_principal_arn = var.kb_admin_principal_arn != "" ? var.kb_admin_principal_arn : data.aws_iam_session_context.current.issuer_arn

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
