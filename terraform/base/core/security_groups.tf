# ---------------------------------------------------------------- security groups
# 受信ルールは置かない。ブラウザは SSM のポートフォワーディングで来る（SSM Agent が内側から ssmmessages へつなぎに行く）
resource "aws_security_group" "web" {
  name        = "${var.name_prefix}-web"
  description = "Chat web EC2 - no inbound, outbound HTTPS only"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-web" }
}

resource "aws_vpc_security_group_egress_rule" "web_https" {
  security_group_id = aws_security_group.web.id
  description       = "HTTPS to VPC endpoints"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "runtime" {
  name        = "${var.name_prefix}-runtime"
  description = "AgentCore Runtime ENIs - outbound HTTPS only"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-runtime" }
}

# VPC endpoints と S3 gateway（S3 のパブリック IP 帯）へ出る。NAT が無いのでインターネットには出られない
resource "aws_vpc_security_group_egress_rule" "runtime_https" {
  security_group_id = aws_security_group.runtime.id
  description       = "HTTPS to VPC endpoints and S3 gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# 条件を付けない。terraform/pipeline/lab / terraform/pipeline/stream が remote state で受け取り、443 の受信ルールを足すため（出力は常に要る）
resource "aws_security_group" "endpoints" {
  name        = "${var.name_prefix}-endpoints"
  description = "Interface endpoints created by terraform/base/core - HTTPS from the chat web and the runtime"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_web" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS from chat web EC2"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.web.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_runtime" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS from AgentCore Runtime ENIs"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.runtime.id
}

resource "aws_vpc_security_group_egress_rule" "endpoints_none" {
  security_group_id = aws_security_group.endpoints.id
  description       = "No outbound"
  ip_protocol       = "-1"
  cidr_ipv4         = "127.0.0.1/32"
}

# 付けるのは ssm / ssmmessages だけ。利用者の PC から bedrock-agentcore や ecr のエンドポイントは見せない
resource "aws_security_group" "client" {
  count = var.create_ssm_endpoints && var.client_cidr != "" ? 1 : 0

  name        = "${var.name_prefix}-client"
  description = "ssm and ssmmessages endpoints - HTTPS from corporate PCs"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-client" }
}

resource "aws_vpc_security_group_ingress_rule" "client_https" {
  count = length(aws_security_group.client)

  security_group_id = aws_security_group.client[0].id
  description       = "HTTPS from corporate PCs running aws ssm start-session"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.client_cidr
}

resource "aws_vpc_security_group_egress_rule" "client_none" {
  count = length(aws_security_group.client)

  security_group_id = aws_security_group.client[0].id
  description       = "No outbound"
  ip_protocol       = "-1"
  cidr_ipv4         = "127.0.0.1/32"
}
