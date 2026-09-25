# ---------------------------------------------------------------- security groups
# SG は 2 つだけ。
#   internal   VPC の中のワークロード全部（web EC2 / lab / Telegraf / Runtime / MSK / Neptune / EMR / Fargate / Lambda）に付ける。
#              受信は VPC の中から（var.vpc_cidr）だけ、送信は自由（NAT から外へ出る。相手ごとの絞り込みは IAM で行う:
#              MSK / Neptune は IAM 認証、S3 / ECR / Bedrock はロールのポリシー）。
#              lab の管理ネットワーク（203.0.113.0/24。trap の送り元）からの受信は terraform/pipeline/lab が足す。
#              EMR Serverless は 0.0.0.0/0 の受信ルールがある SG を拒むので、受信は VPC の CIDR で書く
#   endpoints  VPC の中に残すエンドポイント（OpenSearch Serverless の VPC エンドポイント。terraform/pipeline/analytics）に付ける。
#              受信は internal からの 443 だけ、送信は無し。PrivateLink に戻すときはインターフェース型エンドポイントにもこれを付ける
# インターネットからの受信は SG 以前に経路が無い（vpc.tf: private subnet は IGW に向かない）。
# 2026-09-26 まではワークロードごとの SG（12 個）と相互参照のルール（46 本）だった。戻すときは 7c42b0f を見る
resource "aws_security_group" "internal" {
  name        = "${local.name_prefix}-internal"
  description = "Every workload in the VPC - inbound from the VPC only, outbound free (NAT)"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-internal" }
}

resource "aws_vpc_security_group_ingress_rule" "internal_from_vpc" {
  security_group_id = aws_security_group.internal.id
  description       = "Everything from inside the VPC"
  ip_protocol       = "-1"
  cidr_ipv4         = var.vpc_cidr
}

resource "aws_vpc_security_group_egress_rule" "internal_all" {
  security_group_id = aws_security_group.internal.id
  description       = "Everything (S3 gateway endpoint, NAT Gateway, the VPC)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name_prefix}-endpoints"
  description = "VPC endpoints - HTTPS from the internal SG, no outbound"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_internal" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS from the workloads"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.internal.id
}

resource "aws_vpc_security_group_egress_rule" "endpoints_none" {
  security_group_id = aws_security_group.endpoints.id
  description       = "No outbound"
  ip_protocol       = "-1"
  cidr_ipv4         = "127.0.0.1/32"
}
