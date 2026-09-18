# ---------------------------------------------------------------- VPC endpoints for the AgentCore Runtime (2 AZ)
# VPC / サブネット / SG は terraform/base/core のもの。Runtime の ENI が 2 AZ に置かれるので、Runtime が使うエンドポイントは 2 AZ に置く。
# ecr.api / ecr.dkr / logs は terraform/base/core が作る（lab / analytics / workflow も使うので。2026-09-18 に移した）。ここは Runtime だけが使う bedrock-runtime
# エンドポイントの SG（terraform/base/core の endpoints）は web / runtime の SG からの 443 を通す
locals {
  runtime_endpoint_services = var.create_runtime_endpoints ? {
    "bedrock-runtime" = "bedrock-runtime"
  } : {}
}

resource "aws_vpc_endpoint" "runtime" {
  for_each = local.runtime_endpoint_services

  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = local.subnet_ids
  security_group_ids  = [local.endpoint_sg_id]

  tags = { Name = "${local.name_prefix}-${each.key}" }
}

# Retrieve（Knowledge Base）の経路。KB を作らないときは要らない
resource "aws_vpc_endpoint" "bedrock_agent_runtime" {
  count = local.kb && var.create_kb_endpoint ? 1 : 0

  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.bedrock-agent-runtime"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = local.subnet_ids
  security_group_ids  = [local.endpoint_sg_id]

  tags = { Name = "${local.name_prefix}-bedrock-agent-runtime" }
}

# ---------------------------------------------------------------- VPC endpoint the chat web EC2 invokes the runtime through (1 AZ)
resource "aws_vpc_endpoint" "agentcore" {
  count = var.create_agentcore_endpoint ? 1 : 0

  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.bedrock-agentcore"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [local.instance_subnet]
  security_group_ids  = [local.endpoint_sg_id]

  tags = { Name = "${local.name_prefix}-bedrock-agentcore" }
}
