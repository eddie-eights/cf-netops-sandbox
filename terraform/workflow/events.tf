# ---------------------------------------------------------------- EventBridge -> SQS (the Spark job of terraform/pipeline/analytics opens an anomaly, the worker starts a workflow)
# 異常の検知（Spark）→ EventBridge → エージェントの原因分析 → 修復の提案 → 人の承認 → Temporal で実行、という流れの入口。
# Spark の driver（terraform/pipeline/analytics）が既定のバスに Source <接頭辞>.spark / DetailType AnomalyOpened を put_events し、ここのルールが SQS に流す。
# worker（workflow/worker.py の starter）は SQS を long polling して investigate-<anomaly_id> のワークフローを起こす（キューが無ければ従来どおりテーブルを polling）。
# SQS を挟む理由: ECS のタスクは EventBridge から直接叩けない（API ターゲットも Lambda も要らない一番安い経路。SQS は 100 万リクエスト/月まで無料）。

resource "aws_cloudwatch_event_rule" "anomalies" {
  name        = "${local.name_prefix}-anomalies"
  description = "AnomalyOpened from the Spark job (terraform/pipeline/analytics) to the workflow queue"

  event_pattern = jsonencode({
    # Source は接頭辞ごとに変わる（terraform/pipeline/analytics の locals.event_source が Spark に --event-source で渡す値）。
    # ここを netops.spark で固定すると、1 つの AWS アカウントを何人かで使ったとき他の人の異常でこのワークフローが動く
    source        = ["${local.name_prefix}.spark"]
    "detail-type" = ["AnomalyOpened"]
  })
}

resource "aws_sqs_queue" "anomalies_dlq" {
  name                      = "${local.name_prefix}-anomalies-dlq"
  message_retention_seconds = 1209600 # 14 日（最大）。worker が 5 回受け取っても消さなかったものが来る
}

resource "aws_sqs_queue" "anomalies" {
  name                       = "${local.name_prefix}-anomalies"
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

# worker から SQS へは terraform/base/core の NAT Gateway から出る（2026-09-26 までは sqs の interface endpoint。7c42b0f）
