# ---------------------------------------------------------------- security group of the Spark workers
# EMR Serverless は inbound に 0.0.0.0/0 が開いた SG を拒否する（AWS ドキュメント「Configuring VPC access」、2026-09-17 確認）。
# ここは inbound を自分自身からだけにし、outbound は 443（S3 ゲートウェイ・S3 Tables / logs / events / ssm のエンドポイント）と 9098（MSK）と 8182（Neptune）だけ
# （splunk の格納先を選び、HEC のポートが 443 以外なら、そのポートも。下の emr_splunk）
resource "aws_security_group" "emr" {
  name        = "${local.name_prefix}-emr"
  description = "EMR Serverless workers - Kafka IAM (9098) to MSK, HTTPS to the S3 gateway and the VPC endpoints"
  vpc_id      = local.vpc_id

  tags = { Name = "${local.name_prefix}-emr" }

  lifecycle {
    precondition {
      condition     = local.msk_cluster_arn != "" && local.msk_sg_id != "" && local.bootstrap != ""
      error_message = "terraform/pipeline/stream の state（terraform/pipeline/stream/terraform.tfstate）から msk_cluster_arn / msk_security_group_id / bootstrap_brokers が読めない。terraform/pipeline/stream を先に apply する。"
    }
    precondition {
      condition     = local.neptune_host != "" && local.neptune_sg_id != "" && local.neptune_resource_id != ""
      error_message = "terraform/pipeline/graph の state（terraform/pipeline/graph/terraform.tfstate）から cluster_endpoint / neptune_security_group_id / cluster_resource_id が読めない。検知は異常の「いま」を Neptune に書くので、terraform/pipeline/graph を先に apply する（2026-09-24 から）。"
    }
    precondition {
      condition     = !local.sink_splunk || var.splunk_hec_url != ""
      error_message = "sinks に splunk があるのに splunk_hec_url が空。HEC の URL（https://<host>:8088）を渡す（ops/up.sh なら deploy.env の SPLUNK_HEC_URL）。"
    }
  }
}

# splunk の HEC は 8088 が既定で、上の 443 の egress では届かない。URL のポートが 443 以外ならそのポートを開ける。
# VPC に NAT も IGW も無いので、開けても届く先は VPC の中と DX / VPN の先と PrivateLink だけ（var.splunk_hec_url の description）
resource "aws_vpc_security_group_egress_rule" "emr_splunk" {
  count = local.sink_splunk && local.splunk_hec_port != 443 ? 1 : 0

  security_group_id = aws_security_group.emr.id
  description       = "Splunk HTTP Event Collector (splunk sink)"
  ip_protocol       = "tcp"
  from_port         = local.splunk_hec_port
  to_port           = local.splunk_hec_port
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "emr_self" {
  security_group_id            = aws_security_group.emr.id
  description                  = "Driver and executors of one job talk to each other"
  ip_protocol                  = "tcp"
  from_port                    = 0
  to_port                      = 65535
  referenced_security_group_id = aws_security_group.emr.id
}

resource "aws_vpc_security_group_egress_rule" "emr_self" {
  security_group_id            = aws_security_group.emr.id
  description                  = "Driver and executors of one job talk to each other"
  ip_protocol                  = "tcp"
  from_port                    = 0
  to_port                      = 65535
  referenced_security_group_id = aws_security_group.emr.id
}

resource "aws_vpc_security_group_egress_rule" "emr_https" {
  security_group_id = aws_security_group.emr.id
  description       = "S3 gateway endpoint (script, jars, checkpoint, logs, table data) and the s3tables / logs / events interface endpoints"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "emr_kafka" {
  security_group_id            = aws_security_group.emr.id
  description                  = "Kafka IAM to the MSK brokers"
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
  referenced_security_group_id = local.msk_sg_id
}

# MSK 側（terraform/pipeline/stream の SG）に、この SG からの 9098 を開ける
resource "aws_vpc_security_group_ingress_rule" "msk_from_emr" {
  security_group_id            = local.msk_sg_id
  description                  = "Kafka IAM from the EMR Serverless workers (terraform/pipeline/analytics)"
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
  referenced_security_group_id = aws_security_group.emr.id
}

# 検知が Neptune（terraform/pipeline/graph）の anomaly の頂点を読み書きする。Neptune の SG にも、この SG からの 8182 を開ける
resource "aws_vpc_security_group_egress_rule" "emr_neptune" {
  security_group_id            = aws_security_group.emr.id
  description                  = "Gremlin over HTTPS to Neptune (anomaly vertices)"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = local.neptune_sg_id
}

resource "aws_vpc_security_group_ingress_rule" "neptune_from_emr" {
  security_group_id            = local.neptune_sg_id
  description                  = "Gremlin from the EMR Serverless workers (terraform/pipeline/analytics, anomaly detection)"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = aws_security_group.emr.id
}

# terraform/base/core のエンドポイント SG（logs など）と、下の s3tables エンドポイントに、この SG からの 443 を開ける
resource "aws_vpc_security_group_ingress_rule" "endpoints_from_emr" {
  security_group_id            = local.endpoint_sg_id
  description                  = "EMR Serverless workers through the endpoints of terraform/base/core and the s3tables endpoint"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.emr.id
}

# ---------------------------------------------------------------- VPC endpoint for S3 Tables (control plane)
# Iceberg のカタログ（メタデータの読み書き）は s3tables の API に行く。NAT が無いので interface endpoint。
# データ本体（parquet と metadata.json）は S3 の API で、terraform/base/core の S3 ゲートウェイエンドポイントを通る（ポリシーに s3tables:* のテーブル ARN の許可がある。s3:* の *--table-s3 だけでは 403）。
# 2 AZ に置くのは、EMR Serverless のジョブがどちらのサブネットで動くか選べないため（1 本 $0.014/h/AZ、2026-09-15 確認）
# 証跡（anomaly_events と、terraform/workflow の worker が書く proposal_events）があるので、iceberg を選ばなくても作る
resource "aws_vpc_endpoint" "s3tables" {
  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.s3tables"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = slice(local.subnet_ids, 0, 2)
  security_group_ids  = [local.endpoint_sg_id]
  private_dns_enabled = true

  tags = { Name = "${local.name_prefix}-s3tables" }
}

# ---------------------------------------------------------------- VPC endpoint for EventBridge (PutEvents)
# 検知クエリの driver が新しい異常を put_events する。NAT が無いので interface endpoint、EMR と同じ理由で 2 AZ（+$0.028/h）。
resource "aws_vpc_endpoint" "events" {
  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.events"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = slice(local.subnet_ids, 0, 2)
  security_group_ids  = [local.endpoint_sg_id]
  private_dns_enabled = true

  tags = { Name = "${local.name_prefix}-events" }
}

# 2026-09-24 までは s3tables のエンドポイントも iceberg のときだけ（count）だった
moved {
  from = aws_vpc_endpoint.s3tables[0]
  to   = aws_vpc_endpoint.s3tables
}
