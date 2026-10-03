# ---------------------------------------------------------------- alerts: Grafana / Splunk -> SNS -> SQS (workflow) / Lambda (graph status)
# 検知は Grafana のアラートルール（メトリクス）と Splunk の保存済みサーチ（ログ・trap・テレメトリ）が行い、どちらも同じ形の JSON をこのトピックへ publish する
# （形は workflow/rules.py の alerts_from_message）。受け手は 2 つで、どちらも自分のルートがサブスクリプションを作る:
#   - terraform/workflow        SQS（<接頭辞>-anomalies）→ worker の starter がワークフローを起こす / 解消のシグナルを送る
#   - terraform/pipeline/graph  Lambda（<接頭辞>-graph-status）→ Neptune の機器・回線の status を書き換える
# 2026-10-02 までは Spark の detect が EventBridge に put_events し、ルールが同じ 2 つへ流していた。
# トピックをここ（土台）に置くのは、送り手（analytics）と受け手（workflow / graph）のどれが先に作られても参照できるようにするため。
# トピックに時間課金は無い（publish 100 万件/月まで無料）ので、機能を作らないときも作る。
# 送り手の Grafana / Splunk のタスクは VPC の sns のインターフェース型エンドポイントを通る（ops/up.sh が、Grafana のアラート（GRAFANA と SINK_PROMETHEUS）か SINK_SPLUNK があるときに作らせる）
resource "aws_sns_topic" "alerts" {
  name = "${local.name_prefix}-alerts"

  # 保存時の暗号化は AWS 管理の鍵（aws/sns）。鍵のポリシーが「このアカウントで SNS を通した呼び出し」を許すので、送り手のロールに kms の許可は要らない。
  # SQS / Lambda への配信は SNS が復号してから行う
  kms_master_key_id = "alias/aws/sns"
}

data "aws_iam_policy_document" "alerts_topic" {
  # このアカウントの IAM に任せる（publish できるのは sns:Publish を持つ Grafana / Splunk のタスクロールとデプロイする人）
  statement {
    sid       = "OwnAccount"
    actions   = ["sns:Publish", "sns:Subscribe", "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:ListSubscriptionsByTopic"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }

  # perimeter.tf の資源側。VPC の外からの publish（偽のアラートでワークフローを起こす / status を書き換える）を拒む。
  # 拒むのは Publish だけ（トピックのポリシーの読み書きは、デプロイする人が変わっても戻せるように残す）
  dynamic "statement" {
    for_each = var.network_perimeter ? [1] : []
    content {
      sid       = "DenyOutsideVpc"
      effect    = "Deny"
      actions   = ["sns:Publish"]
      resources = [aws_sns_topic.alerts.arn]

      principals {
        type        = "*"
        identifiers = ["*"]
      }

      condition {
        test     = "StringNotEqualsIfExists"
        variable = "aws:SourceVpc"
        values   = [aws_vpc.this.id]
      }
      condition {
        test     = "BoolIfExists"
        variable = "aws:ViaAWSService"
        values   = ["false"]
      }
      condition {
        test     = "Bool"
        variable = "aws:PrincipalIsAWSService"
        values   = ["false"]
      }
      condition {
        test     = "ArnNotLike"
        variable = "aws:PrincipalArn"
        values   = local.perimeter_exempt_principals
      }
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}
