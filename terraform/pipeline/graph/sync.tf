# ---------------------------------------------------------------- dynamic status: EventBridge (<prefix>.spark) -> Lambda -> Neptune
# The Spark job of terraform/pipeline/analytics puts AnomalyOpened / AnomalyResolved on the default bus when a link goes down / comes back.
# This rule sends both to a small Lambda in the VPC (graph/status_handler.py + agent/graph.py) that sets the property "status"
# (DOWN / UP, ALARM for other traps) on the link edge or the device vertex. The web draws DOWN in red and the chat tools return it.
# The static topology itself comes from lab/ (ops/up.sh 8-2 and ops/sync-graph.sh seed it through the web EC2) - not from here.
# Cost: the rule is free, the Lambda is a few invocations per anomaly (free tier), no NAT and no new interface endpoint
# (Neptune is in the VPC; the Lambda service writes its logs without going through the VPC).

data "archive_file" "status" {
  type        = "zip"
  output_path = "${path.module}/.build/status.zip"

  source {
    content  = file("${path.module}/../../../graph/status_handler.py")
    filename = "index.py"
  }

  source {
    content  = file("${path.module}/../../../agent/graph.py")
    filename = "graph.py"
  }

  # graph.py が import する共通部品（リージョン・SSM パラメータ・boto3 クライアント）。入れ忘れると
  # apply も plan も通るのに、実行時に ModuleNotFoundError で status が一度も書かれない
  source {
    content  = file("${path.module}/../../../agent/toolkit.py")
    filename = "toolkit.py"
  }
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "status" {
  name               = "${local.name_prefix}-graph-status"
  description        = "Status Lambda of terraform/pipeline/graph - writes the dynamic status of devices and links into Neptune"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "status" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${local.name_prefix}-graph-status:*"]
  }

  # VPC の中で動くので ENI を作る（AWSLambdaVPCAccessExecutionRole と同じ中身。マネージドポリシーは付けない）
  statement {
    sid       = "VpcEni"
    actions   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]
    resources = ["*"]
  }

  # status を書き換える property('status', ...) は既存の値の削除を伴うので、Neptune は DeleteDataViaQuery も要る
  # （無いと ExecuteGremlinQuery が AccessDeniedException になり、検知がトポロジに反映されない。2026-09-18 実機）
  statement {
    sid       = "Gremlin"
    actions   = ["neptune-db:ReadDataViaQuery", "neptune-db:WriteDataViaQuery", "neptune-db:DeleteDataViaQuery", "neptune-db:GetQueryStatus"]
    resources = ["arn:${local.partition}:neptune-db:${var.region}:${local.account_id}:${aws_neptune_cluster.graph.cluster_resource_id}/*"]
  }
}

resource "aws_iam_role_policy" "status" {
  name   = "${local.name_prefix}-graph-status"
  role   = aws_iam_role.status.name
  policy = data.aws_iam_policy_document.status.json
}

# 送信は Neptune の 8182 だけ（SSM は引かない。エンドポイントは環境変数で渡す）
resource "aws_security_group" "status" {
  name        = "${local.name_prefix}-graph-status"
  description = "${local.name_prefix} status Lambda - 8182 to Neptune"
  vpc_id      = local.vpc_id

  tags = { Name = "${local.name_prefix}-graph-status" }
}

resource "aws_vpc_security_group_egress_rule" "status_to_neptune" {
  security_group_id            = aws_security_group.status.id
  description                  = "Gremlin to Neptune"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = aws_security_group.neptune.id
}

resource "aws_vpc_security_group_ingress_rule" "neptune_from_status" {
  security_group_id            = aws_security_group.neptune.id
  description                  = "status Lambda"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = aws_security_group.status.id
}

resource "aws_cloudwatch_log_group" "status" {
  name              = "/aws/lambda/${local.name_prefix}-graph-status"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "status" {
  function_name    = "${local.name_prefix}-graph-status"
  role             = aws_iam_role.status.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "index.handler"
  filename         = data.archive_file.status.output_path
  source_code_hash = data.archive_file.status.output_base64sha256
  timeout          = 30
  memory_size      = 128

  # Neptune と同じサブネット。NAT が無いので外には出ない（出る必要も無い）
  vpc_config {
    subnet_ids         = local.subnet_ids
    security_group_ids = [aws_security_group.status.id]
  }

  environment {
    variables = {
      NEPTUNE_ENDPOINT = "${aws_neptune_cluster.graph.endpoint}:${aws_neptune_cluster.graph.port}" # graph.py はこれがあれば SSM を引かない
    }
  }

  depends_on = [aws_cloudwatch_log_group.status, aws_iam_role_policy.status, aws_neptune_cluster_instance.graph]
}

resource "aws_cloudwatch_event_rule" "status" {
  name        = "${local.name_prefix}-graph-status"
  description = "AnomalyOpened / AnomalyResolved from the Spark job (terraform/pipeline/analytics) to the status Lambda"

  event_pattern = jsonencode({
    # Source は接頭辞ごとに変わる（terraform/pipeline/analytics の locals.event_source が Spark に --event-source で渡す値）。
    # ここを netops.spark で固定すると、1 つの AWS アカウントを何人かで使ったとき他の人の異常でこの Lambda が動く
    source        = ["${local.name_prefix}.spark"]
    "detail-type" = ["AnomalyOpened", "AnomalyResolved"]
  })
}

resource "aws_cloudwatch_event_target" "status" {
  rule      = aws_cloudwatch_event_rule.status.name
  target_id = "graph-status"
  arn       = aws_lambda_function.status.arn

  # 配信に失敗したら 3 回まで 5 分の間隔で試す（Lambda 側の失敗は Lambda の非同期呼び出しが 2 回まで再試行する）
  retry_policy {
    maximum_event_age_in_seconds = 900
    maximum_retry_attempts       = 3
  }
}

resource "aws_lambda_permission" "status" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.status.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.status.arn
}
