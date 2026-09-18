# ---------------------------------------------------------------- AgentCore Runtime execution role
# ロールだけをここに置く。Runtime 本体とその実行ポリシーは terraform/agent（機能 agent）。terraform/pipeline/stream / terraform/pipeline/graph は
# このロール名を state から読み、anomaly テーブルや Neptune を読むポリシーを足すので、ロールは agent より長生きする土台に置く
resource "aws_iam_role" "runtime" {
  name        = "${var.name_prefix}-runtime"
  description = "Execution role for the ${var.name_prefix} AgentCore Runtime"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:*" }
      }
    }]
  })

  tags = { Name = "${var.name_prefix}-runtime" }
}
