# netops-poc - PIPELINE analytics root module. A Spark streaming job on EMR Serverless reads the Telegraf messages
# (topics metrics / traps) from MSK (terraform/pipeline/stream) and stores them in S3 Tables (Iceberg, all topics), OpenSearch Serverless
# (log topics) and Amazon Managed Service for Prometheus (metric topics) - see var.sinks. The same job detects link_down / trap anomalies,
# writes them to the DynamoDB table of terraform/pipeline/stream and puts an AnomalyOpened event on the default EventBridge bus (terraform/workflow listens).
# The table bucket is the long-term record of the pipeline; DynamoDB (terraform/pipeline/stream) keeps only the current anomalies.
# Costs about 0.17 USD per hour while the streaming job runs (docs/cost.md) - ops/down.sh cancels the job and destroys this root.

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / バケットは terraform/base/core、MSK は terraform/pipeline/stream の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/core/terraform.tfstate"
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

  # ops/up.sh が置く場所（docs/pipeline.md の a-1）。スクリプトと jar は読むだけ、checkpoint と logs は書く
  s3_prefix     = "analytics"
  script_key    = "${local.s3_prefix}/snmp_sinks.py"
  jars_prefix   = "${local.s3_prefix}/jars"
  logs_prefix   = "${local.s3_prefix}/logs"
  checkpoint    = "${local.s3_prefix}/checkpoint"
  catalog_name  = "s3tables"
  log_group     = "/aws/emr-serverless/${local.name_prefix}"
  table_bucket  = "${local.name_prefix}-tables"
  iceberg_table = "${local.catalog_name}.${var.namespace}.${var.table_name}"
  # iceberg を選ばないときはテーブルバケットも s3tables のエンドポイントも作らない（tables.tf / network.tf）
  table_bucket_arn = local.sink_iceberg ? aws_s3tables_table_bucket.tables[0].arn : ""

  # 格納先（sinks.tf。spark/snmp_sinks.py の --sinks と同じ名前）
  sink_iceberg    = contains(var.sinks, "iceberg")
  sink_opensearch = contains(var.sinks, "opensearch")
  sink_prometheus = contains(var.sinks, "prometheus")

  # put_events の Source（spark/snmp_sinks.py の --event-source）。terraform/workflow と terraform/pipeline/graph の
  # ルールが同じ式で待ち受ける。バスは既定の 1 本を共有するので、ここを接頭辞ごとに変えないと他の人の異常が自分のルールに当たる
  event_source = "${local.name_prefix}.spark"

  # どのトピックがメトリクスでどれがログか（spark/snmp_sinks.py の --metric-topics / --log-topics。iceberg は両方、prometheus はメトリクス、opensearch はログ）
  metric_topics = join(",", var.metric_topics)
  log_topics    = join(",", var.log_topics)

  logs_collection   = "${local.name_prefix}-logs"    # OpenSearch Serverless のコレクション（ログ）
  opensearch_index  = "snmp-logs"                    # spark/snmp_sinks.py の OPENSEARCH_INDEX と同じ
  metrics_workspace = "${local.name_prefix}-metrics" # Prometheus のワークスペースの alias（メトリクス）

  opensearch_endpoint = local.sink_opensearch ? aws_opensearchserverless_collection.logs[0].collection_endpoint : ""
  # prometheus_endpoint は https://aps-workspaces.<region>.amazonaws.com/workspaces/<id>/ で終わる
  prometheus_remote_write_url = local.sink_prometheus ? "${aws_prometheus_workspace.metrics[0].prometheus_endpoint}api/v1/remote_write" : ""
}
