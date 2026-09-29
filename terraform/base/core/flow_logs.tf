# ---------------------------------------------------------------- VPC flow logs (2026-09-30)
# VPC の全 ENI の通信（受けたものも拒んだものも）を CloudWatch Logs に残す。security_groups.tf の通信の表に漏れがあると、その通信は
# action = REJECT で出る（Logs Insights の問い合わせは docs/troubleshooting.md）。本番でも Security Hub の基準（EC2.6）が求める。
# 書式は既定のまま（Logs Insights が srcAddr / dstAddr / dstPort / action などの欄を自分で読む）。集約は 60 秒（既定の 10 分だと検証で待たされる）。
# ロールに logs:CreateLogGroup は渡さない。destroy でロググループを消したあとに、配信の遅れで作り直されて残らないように。
# このロールには perimeter.tf の Deny を付けない（書くのは AWS のサービスで、VPC の外から呼ぶ）
resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/${local.name_prefix}/vpc-flow-logs"
  retention_in_days = var.flow_log_retention_days
}

resource "aws_iam_role" "flow_logs" {
  name        = "${local.name_prefix}-vpc-flow-logs"
  description = "VPC flow logs of ${local.name_prefix} - writes to the flow log group only"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:ec2:${var.region}:${local.account_id}:vpc-flow-log/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "flow_logs" {
  name = "write-flow-log-group"
  role = aws_iam_role.flow_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "${aws_cloudwatch_log_group.flow_logs.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:*"
      },
    ]
  })
}

resource "aws_flow_log" "vpc" {
  vpc_id                   = aws_vpc.this.id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow_logs.arn
  iam_role_arn             = aws_iam_role.flow_logs.arn
  max_aggregation_interval = 60

  tags = { Name = "${local.name_prefix}-vpc" }

  depends_on = [aws_iam_role_policy.flow_logs]
}
