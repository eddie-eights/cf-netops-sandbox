# ---------------------------------------------------------------- proposal table (one item per anomaly, "いま" の状態)
# proposal_id = anomaly_id. status: pending → approved / rejected（人）→ applied → verified / failed（ワーカー）, expired（時間切れ）
resource "aws_dynamodb_table" "proposals" {
  name         = "${var.name_prefix}-proposals"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "proposal_id"

  attribute {
    name = "proposal_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "updated_at"
    type = "N"
  }

  global_secondary_index {
    name            = "status-updated_at-index"
    hash_key        = "status"
    range_key       = "updated_at"
    projection_type = "ALL"
  }

  lifecycle {
    precondition {
      condition     = local.anomaly_table != ""
      error_message = "terraform/pipeline/stream の state（terraform/pipeline/stream/terraform.tfstate）から anomaly_table_name が読めない。terraform/pipeline/stream を先に apply する。"
    }
  }
}

# web と Runtime はテーブル名を SSM から引く（agent/proposals.py）
resource "aws_ssm_parameter" "proposal_table" {
  name  = "${local.param_prefix}/proposal-table"
  type  = "String"
  value = aws_dynamodb_table.proposals.name
}

# ---------------------------------------------------------------- access for the chat runtime and the web EC2 (terraform/base/core roles)
# Both read (the chat tool list_proposals and the approval tab), but only the web EC2 writes: approving and rejecting is what a
# person does in the approval tab, so the chat runtime gets no UpdateItem. The HITL line is drawn in IAM, not only in the code.
data "aws_iam_policy_document" "reader_access" {
  statement {
    sid = "Proposals"
    actions = [
      "dynamodb:Query",
      "dynamodb:GetItem",
      "dynamodb:Scan",
    ]
    resources = [
      aws_dynamodb_table.proposals.arn,
      "${aws_dynamodb_table.proposals.arn}/index/*",
    ]
  }

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

  name   = "${var.name_prefix}-workflow-access"
  role   = each.value
  policy = data.aws_iam_policy_document.reader_access.json
}

# 承認・却下を書けるのは web EC2 だけ（agent/proposals.py の decide。pending のときだけ通る ConditionExpression 付き）
data "aws_iam_policy_document" "decide_access" {
  statement {
    sid       = "Decide"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.proposals.arn]
  }
}

resource "aws_iam_role_policy" "decide_access" {
  name   = "${var.name_prefix}-workflow-decide"
  role   = data.terraform_remote_state.main.outputs.web_role_name
  policy = data.aws_iam_policy_document.decide_access.json
}
