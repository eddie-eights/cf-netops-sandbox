# ---------------------------------------------------------------- Spark の格納先（var.sinks で選ぶ。Kafka を 4 つの格納先に分ける）
# iceberg    = 全トピック → tables.tf の S3 Tables（常に作る。テーブルは無料）
# opensearch = ログのトピック → ここで作る OpenSearch Serverless の TIMESERIES コレクション（VPC エンドポイント経由だけ）
# prometheus = メトリクスのトピック → ここで作る Amazon Managed Service for Prometheus のワークスペース（remote write は NAT Gateway 経由）
# splunk     = 全トピック → AWS の外にある Splunk の HTTP Event Collector（var.splunk_hec_url）。Splunk 自体はここでは作らない。
#              ここにあるのは実行ロールの ssm:GetParameter（token。access.tf）と job_driver の引数（outputs.tf）だけ（HEC へは NAT Gateway から出る）。
#              2026-09-26 まで MSK Connect の Splunk Connect for Kafka にする予定だったが、Spark から直接書くことにした（コネクタのワーカー分の費用と VPC エンドポイントが要らない）

# ---------------------------------------------------------------- opensearch
resource "aws_opensearchserverless_security_policy" "logs_encryption" {
  count = local.sink_opensearch ? 1 : 0

  name        = local.logs_collection
  type        = "encryption"
  description = "AWS owned key for the logs collection"

  policy = jsonencode({
    Rules = [{
      ResourceType = "collection"
      Resource     = ["collection/${local.logs_collection}"]
    }]
    AWSOwnedKey = true
  })
}

# main の Knowledge Base のコレクションと違って公開しない。Spark の driver は VPC の中にいるので、この VPC エンドポイントからだけ通す
resource "aws_opensearchserverless_vpc_endpoint" "logs" {
  count = local.sink_opensearch ? 1 : 0

  name               = local.logs_collection
  vpc_id             = local.vpc_id
  subnet_ids         = slice(local.subnet_ids, 0, 2)
  security_group_ids = [local.endpoint_sg_id]
}

resource "aws_opensearchserverless_security_policy" "logs_network" {
  count = local.sink_opensearch ? 1 : 0

  name        = local.logs_collection
  type        = "network"
  description = "Collection reachable only through the VPC endpoint of terraform/pipeline/analytics"

  policy = jsonencode([{
    Rules = [{
      ResourceType = "collection"
      Resource     = ["collection/${local.logs_collection}"]
    }]
    AllowFromPublic = false
    SourceVPCEs     = [aws_opensearchserverless_vpc_endpoint.logs[0].id]
  }])
}

# Spark の実行ロールだけ。インデックスは _bulk の最初の書き込みで作られる（CreateIndex が要る）
resource "aws_opensearchserverless_access_policy" "logs" {
  count = local.sink_opensearch ? 1 : 0

  name        = local.logs_collection
  type        = "data"
  description = "EMR Serverless runtime role writes the snmp-logs index"

  policy = jsonencode([{
    Rules = [
      {
        ResourceType = "collection"
        Resource     = ["collection/${local.logs_collection}"]
        Permission   = ["aoss:CreateCollectionItems", "aoss:DescribeCollectionItems"]
      },
      {
        ResourceType = "index"
        Resource     = ["index/${local.logs_collection}/${local.opensearch_index}*"]
        Permission   = ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:UpdateIndex", "aoss:WriteDocument", "aoss:ReadDocument"]
      },
    ]
    Principal = [aws_iam_role.emr.arn]
  }])
}

# TIMESERIES 型（時系列のログ向け。ドキュメント ID を付けられないので upsert は無い。追記だけ）
# OCU は main の Knowledge Base のコレクションと共有されるか確認できていない（2026-09-17）。共有されなければ最小 1 OCU ≒ $0.24/h が別に掛かる
resource "aws_opensearchserverless_collection" "logs" {
  count = local.sink_opensearch ? 1 : 0

  name             = local.logs_collection
  type             = "TIMESERIES"
  description      = "${local.name_prefix} SNMP traps and logs from the Spark job"
  standby_replicas = "DISABLED"

  tags = { Name = local.logs_collection }

  depends_on = [
    aws_opensearchserverless_security_policy.logs_encryption,
    aws_opensearchserverless_security_policy.logs_network,
    aws_opensearchserverless_access_policy.logs,
  ]
}

# ---------------------------------------------------------------- prometheus
# ワークスペースは無料。取り込んだサンプル数と保存量で課金（Price List、2026-09-17 確認は取れていない）
resource "aws_prometheus_workspace" "metrics" {
  count = local.sink_prometheus ? 1 : 0

  alias = local.metrics_workspace

  tags = { Name = local.metrics_workspace }
}

# remote write の API（aps-workspaces）へは NAT Gateway から出る（2026-09-26 までは interface endpoint。7c42b0f）
