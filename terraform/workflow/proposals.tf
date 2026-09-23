# ---------------------------------------------------------------- proposals（修復案）
# 修復案の「いま」は Neptune の頂点（label proposal、id = <anomaly_id>#<first_seen> で発生ごと）。status: pending → approved / rejected（人）
# → applied → verified / failed（ワーカー）, expired（時間切れ）, obsolete（承認のあいだに異常が閉じた）。
# 作成・承認・却下・適用・確認は 1 行ずつ S3 Tables の proposal_events（terraform/pipeline/analytics の tables.tf）に残る。
# 以前は DynamoDB のテーブルだった（2026-09-24 に Neptune と S3 Tables に寄せた）。
#
# Neptune の読み書きは terraform/pipeline/graph の access.tf が Runtime と web の両方に付ける（neptune-db は頂点のラベル単位で
# 絞れない）。なので「チャットからは承認できない」（HITL）は IAM ではなくコードで守る: agent/proposals.py の decide を呼ぶのは web の承認タブだけで、
# チャットのツール（TOOL_SPECS）には decide が無い。

# ---------------------------------------------------------------- access for the chat runtime and the web EC2 (terraform/base/core roles)
data "aws_iam_policy_document" "reader_access" {
  statement {
    sid       = "Parameters"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${local.param_prefix}/*"]
  }

  dynamic "statement" {
    for_each = var.create_gateway ? [1] : []
    content {
      sid       = "Gateway"
      actions   = ["bedrock-agentcore:InvokeGateway"]
      resources = [aws_bedrockagentcore_gateway.tools[0].gateway_arn]
    }
  }
}

resource "aws_iam_role_policy" "reader_access" {
  for_each = local.reader_role_names

  name   = "${local.name_prefix}-workflow-access"
  role   = each.value
  policy = data.aws_iam_policy_document.reader_access.json
}
