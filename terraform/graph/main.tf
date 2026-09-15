# fukuda-nwc-poc - phase 2 graph root module. One Neptune cluster (IAM auth, one db.t4g.medium instance) holds the network topology
# (device vertices, link edges). The chat runtime and the web read it through boto3 neptunedata (Gremlin); the web can also edit it.
# Without this root module both fall back to the static data in agent/data/. Costs about 0.12 USD per hour while it exists - destroy it the same day.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# VPC / サブネット / SG / ロール名は terraform/main の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../main/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id          = data.terraform_remote_state.main.outputs.vpc_id
  subnet_ids      = data.terraform_remote_state.main.outputs.runtime_subnet_ids
  runtime_sg_id   = data.terraform_remote_state.main.outputs.runtime_security_group_id
  web_sg_id       = data.terraform_remote_state.main.outputs.instance_security_group_id
  reader_role_ids = toset([data.terraform_remote_state.main.outputs.runtime_role_name, data.terraform_remote_state.main.outputs.web_role_name])
}

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

# ---------------------------------------------------------------- neptune
resource "aws_neptune_cluster" "graph" {
  cluster_identifier                  = "${var.name_prefix}-graph"
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
  identifier                 = "${var.name_prefix}-graph-1"
  cluster_identifier         = aws_neptune_cluster.graph.id
  engine                     = "neptune"
  instance_class             = var.instance_class
  auto_minor_version_upgrade = false
  apply_immediately          = true
}

# topology.py / graph.py はここからエンドポイントを読む（環境変数 PARAM_PREFIX = /<name_prefix>。terraform/main が Runtime と Web に渡す）
resource "aws_ssm_parameter" "endpoint" {
  name        = "/${var.name_prefix}/neptune-endpoint"
  type        = "String"
  value       = "${aws_neptune_cluster.graph.endpoint}:${aws_neptune_cluster.graph.port}"
  description = "Neptune writer endpoint host:port for the chat runtime and the web (terraform/graph)"
}

# ---------------------------------------------------------------- access for the roles of terraform/main
resource "aws_iam_role_policy" "graph_access" {
  for_each = local.reader_role_ids

  name = "${var.name_prefix}-graph-access"
  role = each.value

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Gremlin"
        Effect = "Allow"
        Action = [
          "neptune-db:ReadDataViaQuery",
          "neptune-db:WriteDataViaQuery",
          "neptune-db:DeleteDataViaQuery",
          "neptune-db:GetEngineStatus",
          "neptune-db:GetQueryStatus",
          "neptune-db:CancelQuery",
        ]
        Resource = "arn:${local.partition}:neptune-db:${var.region}:${local.account_id}:${aws_neptune_cluster.graph.cluster_resource_id}/*"
      },
      {
        Sid      = "Parameters"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/*"
      },
    ]
  })

  depends_on = [aws_neptune_cluster_instance.graph]
}
