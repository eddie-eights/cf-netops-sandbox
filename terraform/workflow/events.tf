# ---------------------------------------------------------------- EventBridge -> SQS (the Spark job of terraform/pipeline/analytics opens an anomaly, the worker starts a workflow)
# 2026-09-17 ユーザー決定「Spark が異常を検知したら EventBridge にイベント発行して、それを検知した agent が原因分析 → 修復の提案 → 人間の承認 → Temporal で実行」。
# Spark の driver（terraform/pipeline/analytics）が既定のバスに Source netops.spark / DetailType AnomalyOpened を put_events し、ここのルールが SQS に流す。
# worker（workflow/worker.py の starter）は SQS を long polling して investigate-<anomaly_id> のワークフローを起こす（キューが無ければ従来どおりテーブルを polling）。
# SQS を挟む理由: ECS のタスクは EventBridge から直接叩けない（API ターゲットも Lambda も要らない一番安い経路。SQS は 100 万リクエスト/月まで無料）。

resource "aws_cloudwatch_event_rule" "anomalies" {
  name        = "${var.name_prefix}-anomalies"
  description = "AnomalyOpened from the Spark job (terraform/pipeline/analytics) to the workflow queue"

  event_pattern = jsonencode({
    source        = ["netops.spark"]
    "detail-type" = ["AnomalyOpened"]
  })
}

resource "aws_sqs_queue" "anomalies_dlq" {
  name                      = "${var.name_prefix}-anomalies-dlq"
  message_retention_seconds = 1209600 # 14 日（最大）。worker が 5 回受け取っても消さなかったものが来る
}

resource "aws_sqs_queue" "anomalies" {
  name                       = "${var.name_prefix}-anomalies"
  visibility_timeout_seconds = 120 # worker が start_workflow して delete するまで。超えたら別の受信で同じ workflow id を起こす（Temporal が二重起動を弾く）
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20 # long polling（空のときの受信リクエストを減らす）

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.anomalies_dlq.arn
    maxReceiveCount     = 5
  })
}

data "aws_iam_policy_document" "anomalies_queue" {
  statement {
    sid       = "EventBridgeSendMessage"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.anomalies.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.anomalies.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "anomalies" {
  queue_url = aws_sqs_queue.anomalies.id
  policy    = data.aws_iam_policy_document.anomalies_queue.json
}

resource "aws_cloudwatch_event_target" "anomalies" {
  rule      = aws_cloudwatch_event_rule.anomalies.name
  target_id = "workflow-queue"
  arn       = aws_sqs_queue.anomalies.arn

  dead_letter_config {
    arn = aws_sqs_queue.anomalies_dlq.arn
  }
}

# EventBridge がターゲットの DLQ に書けるように（ルール → SQS の配信に失敗したとき）
data "aws_iam_policy_document" "anomalies_dlq" {
  statement {
    sid       = "EventBridgeDeadLetter"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.anomalies_dlq.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.anomalies.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "anomalies_dlq" {
  queue_url = aws_sqs_queue.anomalies_dlq.id
  policy    = data.aws_iam_policy_document.anomalies_dlq.json
}

# worker は VPC の中（NAT 無し）なので SQS へは interface endpoint で届く（1 AZ、タスクのサブネット。$0.014/h）
resource "aws_vpc_endpoint" "sqs" {
  count = var.create_sqs_endpoint ? 1 : 0

  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.sqs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [local.subnet_id]
  security_group_ids  = [local.endpoint_sg_id]
  private_dns_enabled = true

  tags = { Name = "${var.name_prefix}-sqs" }
}
