# fukuda-nwc-poc - phase 3 workflow root module. One ECS on Fargate task (ARM64, 1 vCPU / 2 GB) runs the Temporal dev server
# and a Python worker in the VPC of terraform/main. The worker starts one workflow per open anomaly (terraform/stream),
# asks the chat runtime (AgentCore) for a cause and a fix, writes a proposal to DynamoDB, waits for a human decision
# (web tab "承認"), applies the fix on the lab EC2 (terraform/lab) through SSM Run Command and checks that the anomaly resolved.
# Optionally an AgentCore Gateway (MCP) exposes the agent tools through a Lambda so the runtime can call them over MCP.
# Costs about 0.05 USD per hour while it exists (Fargate) - destroy it the same day.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / ロール名 / Runtime ARN は terraform/main、異常テーブルは terraform/stream、lab EC2 は terraform/lab の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../main/terraform.tfstate"
  }
}

data "terraform_remote_state" "stream" {
  backend = "local"

  config = {
    path = "${path.module}/../stream/terraform.tfstate"
  }
}

data "terraform_remote_state" "lab" {
  backend = "local"

  config = {
    path = "${path.module}/../lab/terraform.tfstate"
  }
}

data "terraform_remote_state" "ecr" {
  backend = "local"

  config = {
    path = "${path.module}/../ecr/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id            = data.terraform_remote_state.main.outputs.vpc_id
  subnet_id         = data.terraform_remote_state.main.outputs.instance_subnet_id # サブネット a（ssm / bedrock-agentcore のエンドポイントがある方）
  endpoint_sg_id    = data.terraform_remote_state.main.outputs.endpoint_security_group_id
  runtime_arn       = data.terraform_remote_state.main.outputs.agent_runtime_arn
  reader_role_names = toset([data.terraform_remote_state.main.outputs.runtime_role_name, data.terraform_remote_state.main.outputs.web_role_name])

  # stream が無いと異常が無い。下の precondition で「stream を先に」と出す
  anomaly_table     = try(data.terraform_remote_state.stream.outputs.anomaly_table_name, "")
  anomaly_table_arn = local.anomaly_table == "" ? "" : "arn:${local.partition}:dynamodb:${var.region}:${local.account_id}:table/${local.anomaly_table}"

  # lab が無ければ Apply の段は打つ先が無い（ワーカーは proposal を failed にする）
  lab_instance_id = try(data.terraform_remote_state.lab.outputs.lab_instance_id, "")

  worker_repository_url   = try(data.terraform_remote_state.ecr.outputs.worker_repository_url, "")
  temporal_repository_url = try(data.terraform_remote_state.ecr.outputs.temporal_repository_url, "")

  worker_image   = "${local.worker_repository_url}:${var.worker_image_tag}"
  temporal_image = "${local.temporal_repository_url}:${var.temporal_image_tag}"

  param_prefix = "/${var.name_prefix}"
  log_group    = "/ecs/${var.name_prefix}-workflow"
}
