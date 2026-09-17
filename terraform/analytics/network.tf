# ---------------------------------------------------------------- security group of the Spark workers
# EMR Serverless は inbound に 0.0.0.0/0 が開いた SG を拒否する（AWS ドキュメント「Configuring VPC access」、2026-09-17 確認）。
# ここは inbound を自分自身からだけにし、outbound は 443（S3 ゲートウェイ・S3 Tables / logs のエンドポイント）と 9098（MSK）だけ
resource "aws_security_group" "emr" {
  name        = "${var.name_prefix}-emr"
  description = "EMR Serverless workers - Kafka IAM (9098) to MSK, HTTPS to the S3 gateway and the VPC endpoints"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name_prefix}-emr" }

  lifecycle {
    precondition {
      condition     = local.msk_cluster_arn != "" && local.msk_sg_id != "" && local.bootstrap != "" && local.anomaly_table != ""
      error_message = "terraform/stream の state（terraform/stream/terraform.tfstate）から msk_cluster_arn / msk_security_group_id / bootstrap_brokers / anomaly_table_name が読めない。terraform/stream を先に apply する。"
    }
  }
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
  description       = "S3 gateway endpoint (script, jars, checkpoint, logs, table data), the DynamoDB gateway endpoint and the s3tables / logs / events interface endpoints"
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

# MSK 側（terraform/stream の SG）に、この SG からの 9098 を開ける
resource "aws_vpc_security_group_ingress_rule" "msk_from_emr" {
  security_group_id            = local.msk_sg_id
  description                  = "Kafka IAM from the EMR Serverless workers (terraform/analytics)"
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
  referenced_security_group_id = aws_security_group.emr.id
}

# terraform/main のエンドポイント SG（logs など）と、下の s3tables エンドポイントに、この SG からの 443 を開ける
resource "aws_vpc_security_group_ingress_rule" "endpoints_from_emr" {
  security_group_id            = local.endpoint_sg_id
  description                  = "EMR Serverless workers through the endpoints of terraform/main and the s3tables endpoint"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.emr.id
}

# ---------------------------------------------------------------- VPC endpoint for S3 Tables (control plane)
# Iceberg のカタログ（メタデータの読み書き）は s3tables の API に行く。NAT が無いので interface endpoint。
# データ本体（parquet）は S3 の API で、terraform/main の S3 ゲートウェイエンドポイントを通る（ポリシーに *--table-s3 の許可がある）。
# 2 AZ に置くのは、EMR Serverless のジョブがどちらのサブネットで動くか選べないため（1 本 $0.014/h/AZ、2026-09-15 確認）
resource "aws_vpc_endpoint" "s3tables" {
  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.s3tables"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = slice(local.subnet_ids, 0, 2)
  security_group_ids  = [local.endpoint_sg_id]
  private_dns_enabled = true

  tags = { Name = "${var.name_prefix}-s3tables" }
}

# ---------------------------------------------------------------- VPC endpoint for EventBridge (PutEvents)
# 検知クエリの driver が新しい異常を put_events する。NAT が無いので interface endpoint、EMR と同じ理由で 2 AZ（+$0.028/h）。
# DynamoDB は terraform/stream のゲートウェイエンドポイント（無料）を通る
resource "aws_vpc_endpoint" "events" {
  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.events"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = slice(local.subnet_ids, 0, 2)
  security_group_ids  = [local.endpoint_sg_id]
  private_dns_enabled = true

  tags = { Name = "${var.name_prefix}-events" }
}
