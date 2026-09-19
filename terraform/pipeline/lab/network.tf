# 操作は SSM Session Manager（SSM Agent が内側から ssmmessages へつなぎに行く）なので、受信ルールは Telegraf の EC2 からの SNMP だけ（telegraf.tf）。
# lab のアドレス（203.0.113.0/24 / 172.16.0.0/16 / 10.x.0.0/24）は EC2 の中の docker network と veth に閉じていて VPC には出ない。
# create_telegraf のときだけ、管理ネットワーク 203.0.113.0/24 を VPC のルートでこの EC2 に向ける（telegraf.tf）
# description は変えると SG が作り直しになり、ほかのルートの SG ルールが参照している間は消せないので、Telegraf の穴を足した今も元の文のまま
resource "aws_security_group" "lab" {
  name        = "${local.name_prefix}-lab"
  description = "Lab EC2 - no inbound, outbound HTTPS only (SSM, ECR, S3 gateway)"
  vpc_id      = local.vpc_id

  tags = { Name = "${local.name_prefix}-lab" }
}

resource "aws_vpc_security_group_egress_rule" "lab_https" {
  security_group_id = aws_security_group.lab.id
  description       = "HTTPS to VPC endpoints and S3 gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_lab" {
  security_group_id            = local.endpoint_sg_id
  description                  = "HTTPS from lab EC2 (ssm, ssmmessages, ecr.api, ecr.dkr of terraform/base/core)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.lab.id
}
