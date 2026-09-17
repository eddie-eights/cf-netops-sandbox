# fukuda-nwc-poc - phase 3 workflow root module. One ECS on Fargate task (ARM64, 1 vCPU / 2 GB) runs the Temporal dev server
# and a Python worker in the VPC of terraform/main. The Spark job of terraform/analytics puts an AnomalyOpened event on EventBridge
# when it opens an anomaly; events.tf routes it to an SQS queue and the worker starts one workflow per anomaly. The workflow asks the
# chat runtime (AgentCore) for a cause and a fix (the runtime looks at Neptune / OpenSearch / Prometheus through the MCP tools),
# writes a proposal to DynamoDB, waits for a human decision (web tab "承認"), applies the fix on the lab EC2 (terraform/lab)
# through SSM Run Command and checks that the anomaly resolved. Temporal runs on ECS now (EKS later - 2026-09-17 user decision).
# The AgentCore Gateway (MCP) exposes the agent tools through a Lambda in the VPC so the runtime can read Neptune, the logs
# collection and the metrics workspace over MCP. Costs about 0.06 USD per hour while it exists (Fargate + endpoints) - destroy it the same day.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / ロール名 / Runtime ARN は terraform/main、異常テーブルは terraform/stream、lab EC2 は terraform/lab、
# Neptune の SG は terraform/graph、OpenSearch / Prometheus は terraform/analytics の state から読む（graph / analytics は無くてもよい）
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

data "terraform_remote_state" "graph" {
  backend = "local"

  config = {
    path = "${path.module}/../graph/terraform.tfstate"
  }
}

data "terraform_remote_state" "analytics" {
  backend = "local"

  config = {
    path = "${path.module}/../analytics/terraform.tfstate"
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

  # graph / analytics が無ければ tools Lambda は異常テーブルと静的トポロジだけを見る（許可も SG の穴も付かない）
  neptune_sg_id       = try(data.terraform_remote_state.graph.outputs.neptune_security_group_id, "")
  neptune_resource_id = try(data.terraform_remote_state.graph.outputs.cluster_resource_id, "")
  neptune_data_arn    = local.neptune_resource_id == "" ? "" : "arn:${local.partition}:neptune-db:${var.region}:${local.account_id}:${local.neptune_resource_id}/*"

  opensearch_collection_name = try(data.terraform_remote_state.analytics.outputs.opensearch_collection_name, "")
  opensearch_collection_arn  = try(data.terraform_remote_state.analytics.outputs.opensearch_collection_arn, "")
  opensearch_endpoint        = try(data.terraform_remote_state.analytics.outputs.opensearch_collection_endpoint, "")
  opensearch_index           = try(data.terraform_remote_state.analytics.outputs.opensearch_index, "snmp-logs")
  prometheus_workspace_arn   = try(data.terraform_remote_state.analytics.outputs.prometheus_workspace_arn, "")
  prometheus_query_url       = try(data.terraform_remote_state.analytics.outputs.prometheus_query_url, "")

  worker_repository_url   = try(data.terraform_remote_state.ecr.outputs.worker_repository_url, "")
  temporal_repository_url = try(data.terraform_remote_state.ecr.outputs.temporal_repository_url, "")

  worker_image   = "${local.worker_repository_url}:${var.worker_image_tag}"
  temporal_image = "${local.temporal_repository_url}:${var.temporal_image_tag}"

  param_prefix = "/${var.name_prefix}"
  log_group    = "/ecs/${var.name_prefix}-workflow"
}
