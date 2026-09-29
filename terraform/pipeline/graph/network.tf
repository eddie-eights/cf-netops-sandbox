# ---------------------------------------------------------------- network
# SG は terraform/base/core の neptune（web / runtime / spark / lambda / workflow の SG から 8182 だけ受け、送信は無い。security_groups.tf の通信の表）。
# Neptune は IAM 認証も掛かる。2026-09-26 まではここに Neptune の SG と相手ごとの 8182 の穴があった（7c42b0f）
resource "aws_neptune_subnet_group" "graph" {
  name        = "${local.name_prefix}-graph"
  description = "${local.name_prefix} Neptune subnets"
  subnet_ids  = local.subnet_ids
}
