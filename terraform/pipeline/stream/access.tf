# ---------------------------------------------------------------- access for the roles of terraform/base/core and terraform/pipeline/lab
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
  role = local.lab_role_name

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
