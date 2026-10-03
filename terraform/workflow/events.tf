# ---------------------------------------------------------------- SNS -> SQS (Grafana / Splunk publish an alert, the worker starts a workflow)
# 異常の検知（Grafana のアラートルールと Splunk の保存済みサーチ）→ SNS → SQS → エージェントの原因分析 → 修復の提案 → 人の承認 → Temporal で実行、という流れの入口。
# トピック（<接頭辞>-alerts）は terraform/base/core の alerts.tf にあり、ここはキューと購読を持つ。メッセージの形は workflow/rules.py の alerts_from_message。
# worker（workflow/worker.py の starter）は SQS を long polling し、firing なら investigate-<anomaly_id> のワークフローを起こし、
# resolved なら走っているワークフローにシグナルを送る。Grafana と Splunk が同じ異常を別々に知らせても、ワークフロー ID が同じなので 1 つにまとまる。
# SQS を挟む理由: ECS のタスクは SNS から直接叩けない（HTTP の口も Lambda も要らない一番安い経路。SQS は 100 万リクエスト/月まで無料）。
# キューのポリシーには VPC の外からの呼び出しを拒む Deny も入れる（terraform/base/core の perimeter.tf の資源側）。SNS は
# aws:PrincipalIsAWSService で外れ、キューの属性（ポリシーを含む）の読み書きはデプロイする人が変わっても戻せるように外す。
# 2026-10-02 までは Spark の detect が EventBridge に put_events し、ここのルールがキューへ流していた

locals {
  sqs_policy_actions = ["sqs:GetQueueAttributes", "sqs:SetQueueAttributes"]
  perimeter_conditions = [
    { test = "StringNotEqualsIfExists", variable = "aws:SourceVpc", values = [local.vpc_id] },
    { test = "BoolIfExists", variable = "aws:ViaAWSService", values = ["false"] },
    { test = "Bool", variable = "aws:PrincipalIsAWSService", values = ["false"] },
    { test = "ArnNotLike", variable = "aws:PrincipalArn", values = local.perimeter_exempt_principals },
  ]
}

resource "aws_sqs_queue" "anomalies_dlq" {
  name                      = "${local.name_prefix}-anomalies-dlq"
  message_retention_seconds = 1209600 # 14 日（最大）。worker が 5 回受け取っても消さなかったものが来る
}

resource "aws_sqs_queue" "anomalies" {
  name                       = "${local.name_prefix}-anomalies"
  visibility_timeout_seconds = 120 # worker が start_workflow / signal して delete するまで。超えたら別の受信で同じ workflow id を起こす（Temporal が二重起動を弾く）
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20 # long polling（空のときの受信リクエストを減らす）

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.anomalies_dlq.arn
    maxReceiveCount     = 5
  })
}

data "aws_iam_policy_document" "anomalies_queue" {
  statement {
    sid       = "SnsSendMessage"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.anomalies.arn]

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.alerts_topic_arn]
    }
  }

  dynamic "statement" {
    for_each = local.perimeter_policy_arn != "" ? [aws_sqs_queue.anomalies.arn] : []
    content {
      sid         = "DenyOutsideVpc"
      effect      = "Deny"
      not_actions = local.sqs_policy_actions
      resources   = [statement.value]
      principals {
        type        = "*"
        identifiers = ["*"]
      }
      dynamic "condition" {
        for_each = local.perimeter_conditions
        content {
          test     = condition.value.test
          variable = condition.value.variable
          values   = condition.value.values
        }
      }
    }
  }
}

resource "aws_sqs_queue_policy" "anomalies" {
  queue_url = aws_sqs_queue.anomalies.id
  policy    = data.aws_iam_policy_document.anomalies_queue.json
}

# 購読。raw_message_delivery で SNS の封筒を外し、Grafana / Splunk が publish した JSON がそのまま SQS の Body になる
# （封筒つきでも workflow/rules.py は読める）。キューへ配れなかったものは DLQ へ
resource "aws_sns_topic_subscription" "anomalies" {
  topic_arn            = local.alerts_topic_arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.anomalies.arn
  raw_message_delivery = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.anomalies_dlq.arn
  })

  # ポリシーが付く前に購読すると、最初の配信が AccessDenied で DLQ にも行けない
  depends_on = [aws_sqs_queue_policy.anomalies, aws_sqs_queue_policy.anomalies_dlq]

  lifecycle {
    precondition {
      condition     = local.alerts_topic_arn != ""
      error_message = "terraform/base/core の state から alerts_topic_arn が読めない（2026-10-02 より前の土台）。terraform/base/core を apply し直す（ops/up.sh）。"
    }
  }
}

# SNS が購読の DLQ に書けるように（トピック → SQS の配信に失敗したとき）
data "aws_iam_policy_document" "anomalies_dlq" {
  statement {
    sid       = "SnsDeadLetter"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.anomalies_dlq.arn]

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.alerts_topic_arn]
    }
  }

  dynamic "statement" {
    for_each = local.perimeter_policy_arn != "" ? [aws_sqs_queue.anomalies_dlq.arn] : []
    content {
      sid         = "DenyOutsideVpc"
      effect      = "Deny"
      not_actions = local.sqs_policy_actions
      resources   = [statement.value]
      principals {
        type        = "*"
        identifiers = ["*"]
      }
      dynamic "condition" {
        for_each = local.perimeter_conditions
        content {
          test     = condition.value.test
          variable = condition.value.variable
          values   = condition.value.values
        }
      }
    }
  }
}

resource "aws_sqs_queue_policy" "anomalies_dlq" {
  queue_url = aws_sqs_queue.anomalies_dlq.id
  policy    = data.aws_iam_policy_document.anomalies_dlq.json
}

# worker から SQS へは terraform/base/core の sqs のインターフェース型エンドポイントを通る（ops/up.sh が WORKFLOW のときに作らせる）
