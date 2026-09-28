# netops-poc - PIPELINE analytics root module. A Spark streaming job on EMR Serverless reads the Telegraf messages
# (topics metrics / traps) from MSK (terraform/pipeline/stream) and stores them in S3 Tables (Iceberg, all topics), OpenSearch Serverless
# (log topics), Amazon Managed Service for Prometheus (metric topics) and, when asked, the HTTP Event Collector of a Splunk
# (all topics; Splunk Enterprise on ECS here in splunk.tf, inside the VPC) - see var.sinks. Grafana OSS on ECS (grafana.tf)
# shows the Prometheus and OpenSearch sinks. The same job detects link_down / trap anomalies,
# keeps the current ones as anomaly vertices in Neptune (terraform/pipeline/graph), appends every open / resolve to the S3 Tables anomaly_events
# (audit trail) and puts AnomalyOpened / AnomalyResolved on the default EventBridge bus (terraform/workflow and terraform/pipeline/graph listen).
# The table bucket is the long-term record of the pipeline (raw messages, anomaly_events, and proposal_events written by terraform/workflow).
# Costs about 0.17 USD per hour while the streaming job runs (+ about 0.02 for Grafana, + about 0.12 for the Splunk on ECS) - ops/down.sh cancels the job and destroys this root.

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / バケットは terraform/base/core、MSK は terraform/pipeline/stream、Neptune は terraform/pipeline/graph の state から読む
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

# Grafana / Splunk のイメージは terraform/base/ecr（ops/up.sh の手順 1 と 2）
data "terraform_remote_state" "ecr" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/ecr/terraform.tfstate"
  }
}

data "terraform_remote_state" "graph" {
  backend = "local"

  config = {
    path = "${path.module}/../graph/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id     = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  # ECS のタスク（Grafana / Splunk）を置くサブネット（web の EC2 と同じ。SSM のポートフォワードは web の EC2 から届く）
  instance_subnet_id = data.terraform_remote_state.main.outputs.instance_subnet_id
  # SSM のポートフォワードの踏み台（Grafana / Splunk の UI。outputs.tf のコマンド）
  web_instance_id = try(data.terraform_remote_state.main.outputs.web_instance_id, "")
  internal_sg_id  = data.terraform_remote_state.main.outputs.internal_security_group_id
  # 土台の OpenSearch Serverless の VPC エンドポイント（create_opensearch_endpoint=true のときだけある。古い state には output が無い）
  aoss_vpce_id = try(data.terraform_remote_state.main.outputs.opensearch_vpc_endpoint_id, "")
  bucket       = data.terraform_remote_state.main.outputs.kb_bucket_name
  bucket_arn   = "arn:${local.partition}:s3:::${local.bucket}"
  # terraform/base/core の perimeter.tf の Deny（VPC エンドポイントを通らない AWS の API を拒む）。NETWORK_PERIMETER=0 か古い state なら空
  perimeter_policy_arn = try(data.terraform_remote_state.main.outputs.network_perimeter_policy_arn, "")
  # リソースポリシーの Deny から外すプリンシパル（デプロイする人と KB のロール）
  perimeter_exempt_principals = try(data.terraform_remote_state.main.outputs.perimeter_exempt_principals, [])

  # stream が無いと読む Kafka が無い。emr.tf の precondition で「stream を先に」と出す
  msk_cluster_arn = try(data.terraform_remote_state.stream.outputs.msk_cluster_arn, "")
  bootstrap       = try(data.terraform_remote_state.stream.outputs.bootstrap_brokers, "")

  # 検知は異常の「いま」を Neptune に書く。graph が無いと検知できないので、emr.tf の precondition で「graph を先に」と出す
  neptune_host        = try(data.terraform_remote_state.graph.outputs.cluster_endpoint, "")
  neptune_endpoint    = "${local.neptune_host}:8182"
  neptune_resource_id = try(data.terraform_remote_state.graph.outputs.cluster_resource_id, "")
  event_bus_arn       = "arn:${local.partition}:events:${var.region}:${local.account_id}:event-bus/${var.event_bus}"

  # arn:aws:kafka:<region>:<account>:cluster/<name>/<uuid> → topic/<name>/<uuid>/* と group/<name>/<uuid>/*
  topic_arns = "${replace(local.msk_cluster_arn, ":cluster/", ":topic/")}/*"
  group_arns = "${replace(local.msk_cluster_arn, ":cluster/", ":group/")}/*"

  # ops/up.sh が置く場所（ops/up.sh の手順 5）。スクリプトと jar は読むだけ、checkpoint と logs は書く
  s3_prefix   = "analytics"
  script_key  = "${local.s3_prefix}/snmp_sinks.py"
  jars_prefix = "${local.s3_prefix}/jars"
  logs_prefix = "${local.s3_prefix}/logs"
  checkpoint  = "${local.s3_prefix}/checkpoint"
  # checkpoint は Kafka の offset を持つので、MSK を作り直すと新しいクラスタの offset と合わない（古い offset を読みに行って止まるか、
  # 新しいトピックの頭を飛ばす）。MSK のクラスタの uuid（ARN の最後）をパスに入れ、クラスタが変われば checkpoint も新しくする
  msk_cluster_uuid = try(element(split("/", local.msk_cluster_arn), 2), "none")
  checkpoint_uri   = "s3://${local.bucket}/${local.checkpoint}/${local.msk_cluster_uuid}/"
  catalog_name     = "s3tables"
  log_group        = "/aws/emr-serverless/${local.name_prefix}"
  table_bucket     = "${local.name_prefix}-tables"
  iceberg_table    = "${local.catalog_name}.${var.namespace}.${var.table_name}"
  # 証跡（tables.tf）。テーブルバケットは iceberg を選ばなくても作る
  anomaly_events_table = "${local.catalog_name}.${var.namespace}.${aws_s3tables_table.anomaly_events.name}"
  table_bucket_arn     = aws_s3tables_table_bucket.tables.arn

  # 格納先（sinks.tf。spark/snmp_sinks.py の --sinks と同じ名前）
  sink_iceberg    = contains(var.sinks, "iceberg")
  sink_opensearch = contains(var.sinks, "opensearch")
  sink_prometheus = contains(var.sinks, "prometheus")
  sink_splunk     = contains(var.sinks, "splunk")

  # splunk: Splunk Enterprise をここの ECS で立てる（splunk.tf。HEC は VPC の中の splunk.<名前空間>:8088）。
  # AWS の外の Splunk（NAT Gateway から出る）は 2026-09-28 にやめた（NAT を作らない）
  splunk_on_ecs = local.sink_splunk
  # 証明書はイメージの自己署名なので検証しない（VPC の中だけの通信で、宛先は Cloud Map の名前）
  splunk_hec_url         = "https://splunk.${local.service_namespace}:8088"
  splunk_skip_tls_verify = true
  # HEC の token を入れた SSM の SecureString（値は Terraform も state も持たない。ジョブが起動時に ssm:GetParameter で読む）。
  # ops/up.sh が作り、タスクが同じ値を SPLUNK_HEC_TOKEN として受けて HEC の token にする
  splunk_token_parameter     = var.splunk_hec_token_parameter != "" ? var.splunk_hec_token_parameter : "/${local.name_prefix}/splunk/hec-token"
  splunk_token_parameter_arn = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${local.splunk_token_parameter}"
  # 管理者のパスワード（ops/up.sh が無ければ作る SecureString。ECS のタスクが secrets で受ける。ops/down.sh が消す）
  splunk_password_parameter  = "/${local.name_prefix}/splunk/admin-password"
  grafana_password_parameter = "/${local.name_prefix}/grafana/admin-password"

  # Grafana は Prometheus か OpenSearch の格納先があるときだけ意味がある
  create_grafana = var.create_grafana && (local.sink_prometheus || local.sink_opensearch)
  # ECS のクラスタと Cloud Map の名前空間（ecs.tf）は Grafana か ECS の Splunk があるときだけ
  create_ecs        = local.create_grafana || local.splunk_on_ecs
  service_namespace = "${local.name_prefix}.internal"

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
