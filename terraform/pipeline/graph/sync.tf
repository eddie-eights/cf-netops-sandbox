# ---------------------------------------------------------------- dynamic status: SNS (<prefix>-alerts) -> Lambda -> Neptune
# The Grafana alert rules and the Splunk saved searches of terraform/pipeline/analytics publish firing / resolved alerts to the SNS topic of
# terraform/base/core (alerts.tf) when a link goes down / comes back. The subscription below sends them to a small Lambda in the VPC
# (graph/status_handler.py + agent/graph.py + workflow/rules.py) that sets the property "status" (DOWN / UP, ALARM for other traps) on the
# link edge or the device vertex. The web draws DOWN in red and the chat tools return it. Neptune holds the topology and this status only -
# the alerts themselves are not stored here (2026-10-02; until then the Spark job put AnomalyOpened / AnomalyResolved on EventBridge).
# The static topology itself comes from lab/ (ops/up.sh 7-3b and ops/sync-graph.sh seed it through the web EC2) - not from here.
# Cost: the subscription is free, the Lambda is a few invocations per alert (free tier), nothing else
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

  # アラートの JSON の読み方（alerts_from_message）は worker と同じものを使う。標準ライブラリしか読まない
  source {
    content  = file("${path.module}/../../../workflow/rules.py")
    filename = "rules.py"
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
  # （無いと ExecuteGremlinQuery が AccessDeniedException になり、アラートがトポロジに反映されない。2026-09-18 実機）
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

  # Neptune と同じサブネット、SG は terraform/base/core の lambda（Neptune の 8182 へ出られる。SSM は引かない。エンドポイントは環境変数で渡す）
  vpc_config {
    subnet_ids         = local.subnet_ids
    security_group_ids = [local.lambda_sg_id]
  }

  environment {
    variables = {
      NEPTUNE_ENDPOINT = "${aws_neptune_cluster.graph.endpoint}:${aws_neptune_cluster.graph.port}" # graph.py はこれがあれば SSM を引かない
    }
  }

  depends_on = [aws_cloudwatch_log_group.status, aws_iam_role_policy.status, aws_neptune_cluster_instance.graph]
}

# SNS は Lambda を非同期で呼ぶ。Lambda の側の失敗は Lambda が 2 回まで再試行し、SNS の側の配信の失敗は SNS が再試行する
resource "aws_lambda_permission" "status" {
  statement_id  = "AllowSns"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.status.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = local.alerts_topic_arn
}

resource "aws_sns_topic_subscription" "status" {
  topic_arn = local.alerts_topic_arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.status.arn

  depends_on = [aws_lambda_permission.status]

  lifecycle {
    precondition {
      condition     = local.alerts_topic_arn != ""
      error_message = "terraform/base/core の state から alerts_topic_arn が読めない（2026-10-02 より前の土台）。terraform/base/core を apply し直す（ops/up.sh）。"
    }
  }
}
