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
data "aws_iam_policy_document" "reader_access" {
  statement {
    sid = "Proposals"
    actions = [
      "dynamodb:Query",
      "dynamodb:GetItem",
      "dynamodb:Scan",
      "dynamodb:UpdateItem",
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
