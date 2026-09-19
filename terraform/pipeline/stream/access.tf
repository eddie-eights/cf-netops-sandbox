# ---------------------------------------------------------------- access for the roles of terraform/base/core and terraform/pipeline/lab (Telegraf EC2)
resource "aws_iam_role_policy" "anomalies_read" {
  for_each = local.reader_role_names

  name = "${local.name_prefix}-anomalies-read"
  role = each.value

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Table"
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan", "dynamodb:UpdateItem"]
        Resource = [aws_dynamodb_table.anomalies.arn, "${aws_dynamodb_table.anomalies.arn}/index/*"]
      },
      {
        Sid      = "Parameters"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${local.name_prefix}/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "stream_produce" {
  name = "${local.name_prefix}-stream-produce"
  # lab の state に Telegraf が無いとき（create_telegraf=false、または lab を先に消した down.sh）は "" になり、role の検証で
  # destroy まで止まる。lab の telegraf.tf と同じ名前で埋めて通す（apply は network.tf の precondition が止める）
  role = coalesce(local.telegraf_role_name, "${local.name_prefix}-telegraf")

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Kafka"
        Effect = "Allow"
        Action = [
          "kafka-cluster:Connect",
          "kafka-cluster:DescribeCluster",
          "kafka-cluster:WriteData",
          "kafka-cluster:WriteDataIdempotently",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:CreateTopic",
        ]
        Resource = [aws_msk_cluster.stream.arn, local.topic_arns]
      },
      {
        Sid      = "Bootstrap"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${local.name_prefix}/msk-bootstrap"
      },
    ]
  })
}
