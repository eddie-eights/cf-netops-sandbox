# ---------------------------------------------------------------- access for the roles of terraform/base/core
# 異常の表（DynamoDB）は 2026-09-24 にやめた（異常の「いま」は Neptune、履歴は S3 Tables の anomaly_events）。残るのは SSM の読み取りだけ
resource "aws_iam_role_policy" "parameters_read" {
  for_each = local.reader_role_names

  name = "${local.name_prefix}-stream-parameters-read"
  role = each.value

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Parameters"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${local.name_prefix}/*"
      },
    ]
  })
}

# Telegraf の MSK への書き込みは telegraf.tf のタスクロール（2026-09-28 までは terraform/pipeline/lab の Telegraf の EC2 のロールにここで付けていた）
