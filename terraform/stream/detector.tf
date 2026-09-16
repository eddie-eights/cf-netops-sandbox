# ---------------------------------------------------------------- detector Lambda
resource "aws_iam_role" "detector" {
  name        = "${var.name_prefix}-detector"
  description = "fukuda-nwc-poc detector - read MSK topics through the event source mapping, write the anomaly table"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "detector_msk" {
  role       = aws_iam_role.detector.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaMSKExecutionRole"
}

resource "aws_iam_role_policy" "detector" {
  name = "detector"
  role = aws_iam_role.detector.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "KafkaRead"
        Effect = "Allow"
        Action = [
          "kafka-cluster:Connect",
          "kafka-cluster:DescribeGroup",
          "kafka-cluster:AlterGroup",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:ReadData",
          "kafka-cluster:DescribeClusterDynamicConfiguration",
        ]
        Resource = [aws_msk_cluster.stream.arn, local.topic_arns, local.group_arns]
      },
      {
        Sid      = "Table"
        Effect   = "Allow"
        Action   = "dynamodb:UpdateItem"
        Resource = aws_dynamodb_table.anomalies.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "detector" {
  name              = "/aws/lambda/${var.name_prefix}-detector"
  retention_in_days = var.log_retention_days
}

# stream/detector.py をそのまま index.py として zip にする（apply のたびに中身のハッシュで差分を見る）
data "archive_file" "detector" {
  type        = "zip"
  output_path = "${path.module}/.build/detector.zip"

  source {
    content  = file("${path.module}/../../stream/detector.py")
    filename = "index.py"
  }
}

resource "aws_lambda_function" "detector" {
  function_name    = "${var.name_prefix}-detector"
  description      = "Reads the metrics / traps topics and records link_down and trap anomalies in DynamoDB (code is stream/detector.py)"
  role             = aws_iam_role.detector.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "index.handler"
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.detector.output_path
  source_code_hash = data.archive_file.detector.output_base64sha256

  environment {
    variables = {
      TABLE      = aws_dynamodb_table.anomalies.name
      DEVICE_MAP = var.device_map
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.detector,
    aws_iam_role_policy.detector,
    aws_iam_role_policy_attachment.detector_msk,
  ]
}

resource "aws_lambda_event_source_mapping" "detector" {
  for_each = {
    metrics = { batch_size = 100, window = 5 }
    traps   = { batch_size = 10, window = 1 }
  }

  function_name                      = aws_lambda_function.detector.arn
  event_source_arn                   = aws_msk_cluster.stream.arn
  topics                             = [each.key]
  starting_position                  = "LATEST"
  batch_size                         = each.value.batch_size
  maximum_batching_window_in_seconds = each.value.window

  # ESM の ENI が Lambda / STS / ブローカーへ届いてから作る（NAT が無い）
  depends_on = [
    aws_vpc_endpoint.stream,
    aws_vpc_security_group_ingress_rule.stream_endpoints_from_msk,
    aws_vpc_security_group_egress_rule.msk_https,
    aws_iam_role_policy.detector,
    aws_iam_role_policy_attachment.detector_msk,
  ]
}
