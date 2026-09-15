# ---------------------------------------------------------------- VPC
# この root module が VPC ごと作り、destroy すると VPC ごと消える（消し忘れを残さない）。IGW も NAT も無い閉域で、外へはエンドポイントだけ
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.name_prefix}-vpc" }
}

resource "aws_subnet" "a" {
  vpc_id                  = aws_vpc.this.id
  availability_zone_id    = var.az_id_a
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 0)
  map_public_ip_on_launch = false

  tags = { Name = "${var.name_prefix}-private-a" }
}

resource "aws_subnet" "b" {
  vpc_id                  = aws_vpc.this.id
  availability_zone_id    = var.az_id_b
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 1)
  map_public_ip_on_launch = false

  tags = { Name = "${var.name_prefix}-private-b" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.name_prefix}-private" }
}

resource "aws_route_table_association" "a" {
  subnet_id      = aws_subnet.a.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "b" {
  subnet_id      = aws_subnet.b.id
  route_table_id = aws_route_table.private.id
}

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

# 条件を付けない。terraform/lab / terraform/stream が remote state で受け取り、443 の受信ルールを足すため（出力は常に要る）
resource "aws_security_group" "endpoints" {
  name        = "${var.name_prefix}-endpoints"
  description = "Interface endpoints created by terraform/main - HTTPS from the chat web and the runtime"
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

# ---------------------------------------------------------------- VPC endpoints for AgentCore Runtime (2 AZ)
locals {
  runtime_endpoint_services = var.create_runtime_endpoints ? {
    "ecr-api"         = "ecr.api"
    "ecr-dkr"         = "ecr.dkr"
    "logs"            = "logs"
    "bedrock-runtime" = "bedrock-runtime"
  } : {}

  ssm_endpoint_sg_ids = concat([aws_security_group.endpoints.id], aws_security_group.client[*].id)
}

resource "aws_vpc_endpoint" "runtime" {
  for_each = local.runtime_endpoint_services

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.a.id, aws_subnet.b.id]
  security_group_ids  = [aws_security_group.endpoints.id]

  tags = { Name = "${var.name_prefix}-${each.key}" }
}

resource "aws_vpc_endpoint" "bedrock_agent_runtime" {
  count = var.create_kb_endpoint ? 1 : 0

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.bedrock-agent-runtime"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.a.id, aws_subnet.b.id]
  security_group_ids  = [aws_security_group.endpoints.id]

  tags = { Name = "${var.name_prefix}-bedrock-agent-runtime" }
}

# VPC の中から S3 に出る経路はこれだけ。許すのは 3 つ: この root module のバケット（EC2 が web/ を取る、lab が lab/ を取る、
# MSK Connect が stream/ に書く）、ECR のレイヤー置き場（Runtime のイメージ取得）、AL2023 の dnf リポジトリ（EC2 に python3.13 を入れる）。
# バケットへの操作の絞り込みは各ロールの IAM ポリシーで行う（ここで絞ると 2026-09-15 のように ListBucket が落ちて web/ の取得が失敗する）。
# VPC ごとこの root module のものなので、他のワークロードには影響しない
resource "aws_vpc_endpoint" "s3" {
  count = var.create_s3_gateway_endpoint ? 1 : 0

  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowKbBucket"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.kb.arn, "${aws_s3_bucket.kb.arn}/*"]
      },
      {
        Sid       = "AllowECRLayerAccess"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "arn:${local.partition}:s3:::prod-${var.region}-starport-layer-bucket/*"
      },
      {
        Sid       = "AllowAL2023Repos"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "arn:${local.partition}:s3:::al2023-repos-${var.region}-de612dc2/*"
      },
    ]
  })

  tags = { Name = "${var.name_prefix}-s3" }
}

# ---------------------------------------------------------------- VPC endpoints for the chat web EC2 (1 AZ)
resource "aws_vpc_endpoint" "ssm" {
  for_each = var.create_ssm_endpoints ? toset(["ssm", "ssmmessages"]) : toset([])

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.a.id]
  security_group_ids  = local.ssm_endpoint_sg_ids

  tags = { Name = "${var.name_prefix}-${each.value}" }
}

resource "aws_vpc_endpoint" "agentcore" {
  count = var.create_agentcore_endpoint ? 1 : 0

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.bedrock-agentcore"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.a.id]
  security_group_ids  = [aws_security_group.endpoints.id]

  tags = { Name = "${var.name_prefix}-bedrock-agentcore" }
}
