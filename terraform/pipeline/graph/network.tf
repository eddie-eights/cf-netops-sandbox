# ---------------------------------------------------------------- network
resource "aws_neptune_subnet_group" "graph" {
  name        = "${var.name_prefix}-graph"
  description = "${var.name_prefix} Neptune subnets"
  subnet_ids  = local.subnet_ids
}

# 送信ルールは置かない（aws_security_group は作成時に既定の全許可の送信ルールを消す）。Neptune から外へ出る通信は無い
resource "aws_security_group" "neptune" {
  name        = "${var.name_prefix}-neptune"
  description = "${var.name_prefix} Neptune - 8182 from the runtime and the web"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name_prefix}-neptune" }
}

resource "aws_vpc_security_group_ingress_rule" "neptune_from_runtime" {
  security_group_id            = aws_security_group.neptune.id
  description                  = "AgentCore runtime"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = local.runtime_sg_id
}

resource "aws_vpc_security_group_ingress_rule" "neptune_from_web" {
  security_group_id            = aws_security_group.neptune.id
  description                  = "chat web"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = local.web_sg_id
}

# terraform/base/core の runtime / web の SG は送信が 443 だけなので、8182 の送信をこちらで足す（terraform/workflow の tools と同じ形）。
# 無いと Web の EC2 から Neptune への TCP が落とされ、ops/up.sh の 8-2（seed_graph.py）が接続の待ちで数十分止まる（2026-09-17 に Mac の初回で実測）
resource "aws_vpc_security_group_egress_rule" "runtime_to_neptune" {
  security_group_id            = local.runtime_sg_id
  description                  = "Gremlin to Neptune (terraform/pipeline/graph)"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = aws_security_group.neptune.id
}

resource "aws_vpc_security_group_egress_rule" "web_to_neptune" {
  security_group_id            = local.web_sg_id
  description                  = "Gremlin to Neptune (terraform/pipeline/graph)"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = aws_security_group.neptune.id
}
