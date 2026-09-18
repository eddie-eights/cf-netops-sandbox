# ---------------------------------------------------------------- access for the roles of terraform/base/core
resource "aws_iam_role_policy" "graph_access" {
  for_each = local.reader_role_ids

  name = "${local.name_prefix}-graph-access"
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
        Resource = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${local.name_prefix}/*"
      },
    ]
  })

  depends_on = [aws_neptune_cluster_instance.graph]
}
