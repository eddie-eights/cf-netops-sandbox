# fukuda-nwc-poc - NetOps phase 1 for a closed network. Browser on the user's PC -> SSM Session Manager
# port forwarding -> Gradio web on EC2 (chat + topology figure + device table, 127.0.0.1 only, no inbound rules) ->
# AgentCore Runtime (VPC mode) -> Bedrock Knowledge Base (OpenSearch Serverless, hybrid search), static topology
# tools (list_devices / neighbors / blast_radius) and Bedrock Converse with a guardrail.
# No NAT gateway, no EIP, no public IP, no load balancer. The optional lab (containerlab + FRR) is terraform/lab.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# assumed-role のセッションなら元のロールの ARN、IAM ユーザーならそのままユーザーの ARN になる
data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

# エージェントのイメージの置き場は terraform/ecr の state から読む
data "terraform_remote_state" "ecr" {
  count   = var.agent_image_uri == "" ? 1 : 0
  backend = "local"

  config = {
    path = "${path.module}/../ecr/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  kb_admin_principal_arn = var.kb_admin_principal_arn != "" ? var.kb_admin_principal_arn : data.aws_iam_session_context.current.issuer_arn

  rerank           = var.rerank_model_id != ""
  rerank_model_arn = local.rerank ? "arn:${local.partition}:bedrock:${var.region}::foundation-model/${var.rerank_model_id}" : ""

  agent_image_uri = var.agent_image_uri != "" ? var.agent_image_uri : "${data.terraform_remote_state.ecr[0].outputs.agent_repository_url}:${var.agent_image_tag}"

  collection_name = "${var.name_prefix}-kb"
  index_name      = "kb-index"
}
