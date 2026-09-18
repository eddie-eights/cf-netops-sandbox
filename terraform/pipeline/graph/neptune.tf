# ---------------------------------------------------------------- neptune
resource "aws_neptune_cluster" "graph" {
  cluster_identifier                  = "${local.name_prefix}-graph"
  engine                              = "neptune"
  engine_version                      = var.engine_version
  port                                = 8182
  iam_database_authentication_enabled = true
  storage_encrypted                   = true
  backup_retention_period             = 1
  deletion_protection                 = var.deletion_protection
  neptune_subnet_group_name           = aws_neptune_subnet_group.graph.name
  vpc_security_group_ids              = [aws_security_group.neptune.id]

  # その日に消す使い捨て。最終スナップショットは取らない
  skip_final_snapshot = true
  apply_immediately   = true
}

resource "aws_neptune_cluster_instance" "graph" {
  identifier                 = "${local.name_prefix}-graph-1"
  cluster_identifier         = aws_neptune_cluster.graph.id
  engine                     = "neptune"
  instance_class             = var.instance_class
  auto_minor_version_upgrade = false
  apply_immediately          = true
}

# topology.py / graph.py はここからエンドポイントを読む（環境変数 PARAM_PREFIX = /<接頭辞>。terraform/base/core が Runtime と Web に渡す）
resource "aws_ssm_parameter" "endpoint" {
  name        = "/${local.name_prefix}/neptune-endpoint"
  type        = "String"
  value       = "${aws_neptune_cluster.graph.endpoint}:${aws_neptune_cluster.graph.port}"
  description = "Neptune writer endpoint host:port for the chat runtime and the web (terraform/pipeline/graph)"
}
