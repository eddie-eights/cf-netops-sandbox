# fukuda-nwc-poc - phase 2 analytics root module. A Spark streaming job on EMR Serverless reads the Telegraf messages
# (topics metrics / traps) from MSK (terraform/stream) and stores them in S3 Tables (Iceberg, all topics), OpenSearch Serverless
# (log topics) and Amazon Managed Service for Prometheus (metric topics) - see var.sinks. The same job detects link_down / trap anomalies,
# writes them to the DynamoDB table of terraform/stream and puts an AnomalyOpened event on the default EventBridge bus (terraform/workflow listens).
# The table bucket is the long-term record of the pipeline; DynamoDB (terraform/stream) keeps only the current anomalies.
# Costs about 0.17 USD per hour while the streaming job runs (README) - ops/down.sh cancels the job and destroys this root.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / バケットは terraform/main、MSK は terraform/stream の state から読む
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

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id         = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids     = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  endpoint_sg_id = data.terraform_remote_state.main.outputs.endpoint_security_group_id
  bucket         = data.terraform_remote_state.main.outputs.kb_bucket_name
  bucket_arn     = "arn:${local.partition}:s3:::${local.bucket}"

  # stream が無いと読む Kafka が無い。下の precondition で「stream を先に」と出す
  msk_cluster_arn   = try(data.terraform_remote_state.stream.outputs.msk_cluster_arn, "")
  msk_sg_id         = try(data.terraform_remote_state.stream.outputs.msk_security_group_id, "")
  bootstrap         = try(data.terraform_remote_state.stream.outputs.bootstrap_brokers, "")
  anomaly_table     = try(data.terraform_remote_state.stream.outputs.anomaly_table_name, "")
  anomaly_table_arn = "arn:${local.partition}:dynamodb:${var.region}:${local.account_id}:table/${local.anomaly_table}"
  event_bus_arn     = "arn:${local.partition}:events:${var.region}:${local.account_id}:event-bus/${var.event_bus}"

  # arn:aws:kafka:<region>:<account>:cluster/<name>/<uuid> → topic/<name>/<uuid>/* と group/<name>/<uuid>/*
  topic_arns = "${replace(local.msk_cluster_arn, ":cluster/", ":topic/")}/*"
  group_arns = "${replace(local.msk_cluster_arn, ":cluster/", ":group/")}/*"

  # ops/up.sh が置く場所（README の a-1）。スクリプトと jar は読むだけ、checkpoint と logs は書く
  s3_prefix     = "analytics"
  script_key    = "${local.s3_prefix}/snmp_sinks.py"
  jars_prefix   = "${local.s3_prefix}/jars"
  logs_prefix   = "${local.s3_prefix}/logs"
  checkpoint    = "${local.s3_prefix}/checkpoint"
  catalog_name  = "s3tables"
  log_group     = "/aws/emr-serverless/${var.name_prefix}"
  table_bucket  = "${var.name_prefix}-tables"
  iceberg_table = "${local.catalog_name}.${var.namespace}.${var.table_name}"

  # 格納先（sinks.tf。spark/snmp_sinks.py の --sinks と同じ名前）
  sink_iceberg    = contains(var.sinks, "iceberg")
  sink_opensearch = contains(var.sinks, "opensearch")
  sink_prometheus = contains(var.sinks, "prometheus")

  # どのトピックがメトリクスでどれがログか（spark/snmp_sinks.py の --metric-topics / --log-topics。iceberg は両方、prometheus はメトリクス、opensearch はログ）
  metric_topics = join(",", var.metric_topics)
  log_topics    = join(",", var.log_topics)

  logs_collection   = "${var.name_prefix}-logs"    # OpenSearch Serverless のコレクション（ログ）
  opensearch_index  = "snmp-logs"                  # spark/snmp_sinks.py の OPENSEARCH_INDEX と同じ
  metrics_workspace = "${var.name_prefix}-metrics" # Prometheus のワークスペースの alias（メトリクス）

  opensearch_endpoint = local.sink_opensearch ? aws_opensearchserverless_collection.logs[0].collection_endpoint : ""
  # prometheus_endpoint は https://aps-workspaces.<region>.amazonaws.com/workspaces/<id>/ で終わる
  prometheus_remote_write_url = local.sink_prometheus ? "${aws_prometheus_workspace.metrics[0].prometheus_endpoint}api/v1/remote_write" : ""
}
