# ---------------------------------------------------------------- security groups
# msk の description の「the lab EC2」は Telegraf が lab の EC2 にいたころの文言。description を変えると SG の作り直しになり、
# ブローカーの ENI が付いたままでは消せないので残す（実際に 9098 を許しているのは下の msk_from_telegraf）
resource "aws_security_group" "msk" {
  name        = "${local.name_prefix}-msk"
  description = "MSK brokers - Kafka IAM (9098) from the lab EC2, from MSK Connect and from the EMR Serverless workers (terraform/pipeline/analytics)"
  vpc_id      = local.vpc_id

  tags = { Name = "${local.name_prefix}-msk" }

  lifecycle {
    precondition {
      condition     = local.telegraf_sg_id != "" && local.telegraf_role_name != ""
      error_message = "terraform/pipeline/lab の state（terraform/pipeline/lab/terraform.tfstate）から telegraf_security_group_id / telegraf_role_name が読めない。terraform/pipeline/lab を -var create_telegraf=true で先に apply する（ops/up.sh はそうする）。"
    }
  }
}

resource "aws_vpc_security_group_egress_rule" "msk_https" {
  security_group_id = aws_security_group.msk.id
  description       = "MSK Connect workers - S3 gateway, logs / sts endpoints"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "msk_kafka" {
  security_group_id = aws_security_group.msk.id
  description       = "Kafka between brokers and Connect workers (all carry this group)"
  ip_protocol       = "tcp"
  from_port         = 9092
  to_port           = 9098
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "msk_self" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Brokers and MSK Connect workers talk to each other"
  ip_protocol                  = "tcp"
  from_port                    = 9092
  to_port                      = 9098
  referenced_security_group_id = aws_security_group.msk.id
}

resource "aws_vpc_security_group_ingress_rule" "msk_from_telegraf" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Telegraf EC2 of terraform/pipeline/lab (Kafka IAM)"
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
  referenced_security_group_id = local.telegraf_sg_id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_msk" {
  security_group_id            = local.endpoint_sg_id
  description                  = "MSK Connect worker logs through the endpoints of terraform/base/core"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.msk.id
}

resource "aws_security_group" "stream_endpoints" {
  count = var.create_sts_endpoint ? 1 : 0

  name        = "${local.name_prefix}-stream-endpoints"
  description = "sts interface endpoint - HTTPS from the MSK security group"
  vpc_id      = local.vpc_id

  tags = { Name = "${local.name_prefix}-stream-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "stream_endpoints_from_msk" {
  count = var.create_sts_endpoint ? 1 : 0

  security_group_id            = aws_security_group.stream_endpoints[0].id
  description                  = "HTTPS from the MSK Connect workers"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.msk.id
}

# ---------------------------------------------------------------- VPC endpoints
# MSK Connect のワーカーは MSK のサブネット / SG に置かれ、IAM ロールを引き受けるのに STS へ届く必要がある（NAT が無いので interface endpoint。1 AZ で足りる）。
# 本当に要るかは確認できていない（2026-09-17）。detector Lambda を Spark に寄せたので lambda のエンドポイントは外した
resource "aws_vpc_endpoint" "stream" {
  for_each = var.create_sts_endpoint ? toset(["sts"]) : toset([])

  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [local.subnet_ids[0]]
  security_group_ids  = [aws_security_group.stream_endpoints[0].id]

  tags = { Name = "${local.name_prefix}-${each.key}" }
}

resource "aws_vpc_endpoint" "dynamodb" {
  count = var.create_dynamodb_endpoint ? 1 : 0

  vpc_id            = local.vpc_id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.route_table_ids

  tags = { Name = "${local.name_prefix}-dynamodb" }
}
