# ---------------------------------------------------------------- security groups
# msk の description の「the lab EC2」は Telegraf が lab の EC2 にいたころ、「MSK Connect」は S3 sink があったころ（2026-09-26 に削除）の文言。
# description を変えると SG の作り直しになり、ブローカーの ENI が付いたままでは消せないので残す（実際に 9098 を許しているのは下の msk_from_telegraf）
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

resource "aws_vpc_security_group_egress_rule" "msk_kafka" {
  security_group_id = aws_security_group.msk.id
  description       = "Kafka between the brokers (all carry this group)"
  ip_protocol       = "tcp"
  from_port         = 9092
  to_port           = 9098
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "msk_self" {
  security_group_id            = aws_security_group.msk.id
  description                  = "Brokers talk to each other"
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

# 443 の egress と endpoints 側の ingress、sts のエンドポイントは MSK Connect のワーカー（S3 sink）のためのもので、sink と一緒に 2026-09-26 に外した。
# ブローカー自身はログを MSK が届けるので VPC エンドポイントを通らない

