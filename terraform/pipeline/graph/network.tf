# ---------------------------------------------------------------- network
# SG は terraform/base/core の internal（VPC の中からは何でも受ける。Neptune は IAM 認証なので 8182 の穴を相手ごとに開けない）。
# 2026-09-26 まではここに Neptune の SG と runtime / web / status / EMR / tools ごとの 8182 の穴があった（7c42b0f）
resource "aws_neptune_subnet_group" "graph" {
  name        = "${local.name_prefix}-graph"
  description = "${local.name_prefix} Neptune subnets"
  subnet_ids  = local.subnet_ids
}
