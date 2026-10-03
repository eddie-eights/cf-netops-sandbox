# nwc-poc - workflow root module (feature "workflow"). One ECS on Fargate task (ARM64, 1 vCPU / 2 GB) runs the Temporal dev server
# and a Python worker in the VPC of terraform/base/core. The Grafana alert rules and the Splunk saved searches of terraform/pipeline/analytics
# publish alerts to the SNS topic of terraform/base/core; events.tf subscribes an SQS queue to it and the worker starts one workflow per anomaly
# (and signals it when the alert resolves). The workflow asks the
# chat runtime (AgentCore) for a cause and a fix (the runtime looks at Neptune / OpenSearch / Prometheus through the MCP tools),
# writes a proposal to Neptune (label proposal) and one audit row per step to S3 Tables (proposal_events), waits for a human decision (web tab "承認"), applies the fix on the lab EC2 (terraform/pipeline/lab)
# through SSM Run Command and waits for the resolved alert. Temporal runs on ECS now (EKS later - 2026-09-17 user decision).
# The AgentCore Gateway (MCP) exposes the agent tools through a Lambda in the VPC so the runtime can read Neptune, the logs
# collection and the metrics workspace over MCP. Costs about 0.05 USD per hour while it exists (Fargate) - destroy it the same day.

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / ロール名は terraform/base/core、Runtime ARN は terraform/agent、lab EC2 は terraform/pipeline/lab、
# Neptune（修復案の「いま」）は terraform/pipeline/graph、証跡の S3 Tables と OpenSearch / Prometheus は terraform/pipeline/analytics の state から読む。
# ワーカーは Neptune と証跡が無いと動かないので graph と analytics は必須（ecs.tf の precondition）
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

data "terraform_remote_state" "agent" {
  backend = "local"

  config = {
    path = "${path.module}/../agent/terraform.tfstate"
  }
}

data "terraform_remote_state" "lab" {
  backend = "local"

  config = {
    path = "${path.module}/../pipeline/lab/terraform.tfstate"
  }
}

data "terraform_remote_state" "ecr" {
  backend = "local"

  config = {
    path = "${path.module}/../base/ecr/terraform.tfstate"
  }
}

data "terraform_remote_state" "graph" {
  backend = "local"

  config = {
    path = "${path.module}/../pipeline/graph/terraform.tfstate"
  }
}

data "terraform_remote_state" "analytics" {
  backend = "local"

  config = {
    path = "${path.module}/../pipeline/analytics/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id    = data.terraform_remote_state.main.outputs.vpc_id
  subnet_id = data.terraform_remote_state.main.outputs.instance_subnet_id # サブネット a（Web の EC2 と同じ）
  # SG は古い state の destroy でも評価できるように try（空のまま apply に進まないよう remote_state の postcondition で止める）
  workflow_sg_id = try(data.terraform_remote_state.main.outputs.security_group_ids["workflow"], "")
  lambda_sg_id   = try(data.terraform_remote_state.main.outputs.security_group_ids["lambda"], "") # gateway.tf の tools Lambda
  # terraform/base/core の perimeter.tf の Deny（VPC エンドポイントを通らない AWS の API を拒む）。NETWORK_PERIMETER=0 か古い state なら空
  perimeter_policy_arn = try(data.terraform_remote_state.main.outputs.network_perimeter_policy_arn, "")
  # リソースポリシーの Deny から外すプリンシパル（デプロイする人と KB のロール）
  perimeter_exempt_principals = try(data.terraform_remote_state.main.outputs.perimeter_exempt_principals, [])
  # アラートの SNS トピック（terraform/base/core の alerts.tf）。events.tf のキューが購読する。古い state なら空で、購読の precondition が止める
  alerts_topic_arn = try(data.terraform_remote_state.main.outputs.alerts_topic_arn, "")
  # agent が無いとワークフローが原因を聞く先が無い。下の precondition で「agent を先に」と出す
  runtime_arn = try(data.terraform_remote_state.agent.outputs.agent_runtime_arn, "")

  # SSM とゲートウェイを使う 2 つのロール（チャットの Runtime と Web の EC2）。修復案の読み書き（Neptune）は terraform/pipeline/graph の access.tf が付ける
  web_role_name     = data.terraform_remote_state.main.outputs.web_role_name
  reader_role_names = toset([data.terraform_remote_state.main.outputs.runtime_role_name, local.web_role_name])

  # lab が無ければ Apply の段は打つ先が無い（ワーカーは proposal を failed にする）
  lab_instance_id = try(data.terraform_remote_state.lab.outputs.lab_instance_id, "")

  # Neptune（修復案の「いま」）。graph が無ければ空で、ecs.tf の precondition が「graph を先に」と出す
  neptune_resource_id = try(data.terraform_remote_state.graph.outputs.cluster_resource_id, "")
  neptune_host        = try(data.terraform_remote_state.graph.outputs.cluster_endpoint, "")
  neptune_endpoint    = local.neptune_host == "" ? "" : "${local.neptune_host}:8182"
  neptune_data_arn    = local.neptune_resource_id == "" ? "" : "arn:${local.partition}:neptune-db:${var.region}:${local.account_id}:${local.neptune_resource_id}/*"

  # 修復案の証跡（S3 Tables の proposal_events）。analytics が無ければ空で、ecs.tf の precondition が「analytics を先に」と出す
  audit_bucket_arn           = try(data.terraform_remote_state.analytics.outputs.table_bucket_arn, "")
  audit_namespace            = try(data.terraform_remote_state.analytics.outputs.table_namespace, "")
  proposal_events_table_name = try(data.terraform_remote_state.analytics.outputs.proposal_events_table_name, "")
  proposal_events_table_arn  = try(data.terraform_remote_state.analytics.outputs.proposal_events_table_arn, "")

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

  param_prefix = "/${local.name_prefix}"
  log_group    = "/ecs/${local.name_prefix}-workflow"
}
