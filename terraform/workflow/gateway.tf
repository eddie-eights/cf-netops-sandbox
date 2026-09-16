# ---------------------------------------------------------------- AgentCore Gateway (MCP) + tools Lambda
# The chat runtime (agent/app.py) lists the tools through the gateway URL (SSM <name_prefix>/gateway-url) and calls them over MCP
# instead of its built-in functions. The Lambda runs the same agent/topology.py and agent/anomalies.py, outside the VPC,
# so it reads the anomaly table and the static topology (data/) - not Neptune.

locals {
  tools = jsondecode(file("${path.module}/../../tools/tools.json"))
}

data "archive_file" "tools" {
  count = var.create_gateway ? 1 : 0

  type        = "zip"
  output_path = "${path.module}/.build/tools.zip"

  source {
    content  = file("${path.module}/../../tools/handler.py")
    filename = "index.py"
  }

  source {
    content  = file("${path.module}/../../agent/topology.py")
    filename = "topology.py"
  }

  source {
    content  = file("${path.module}/../../agent/anomalies.py")
    filename = "anomalies.py"
  }

  source {
    content  = file("${path.module}/../../agent/graph.py")
    filename = "graph.py"
  }

  source {
    content  = file("${path.module}/../../agent/data/topology.json")
    filename = "data/topology.json"
  }

  # Lambda には PyYAML が無いので devices.yaml を JSON にして入れる（topology.load_static は devices.json を先に見る）
  source {
    content  = jsonencode(yamldecode(file("${path.module}/../../agent/data/devices.yaml")))
    filename = "data/devices.json"
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

resource "aws_iam_role" "tools" {
  count = var.create_gateway ? 1 : 0

  name               = "${var.name_prefix}-tools"
  description        = "Tools Lambda behind the MCP gateway - reads the anomaly table"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "tools" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-tools:*"]
  }

  statement {
    sid       = "Anomalies"
    actions   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"]
    resources = [local.anomaly_table_arn, "${local.anomaly_table_arn}/index/*"]
  }
}

resource "aws_iam_role_policy" "tools" {
  count = var.create_gateway ? 1 : 0

  name   = "${var.name_prefix}-tools"
  role   = aws_iam_role.tools[0].name
  policy = data.aws_iam_policy_document.tools.json
}

resource "aws_cloudwatch_log_group" "tools" {
  count = var.create_gateway ? 1 : 0

  name              = "/aws/lambda/${var.name_prefix}-tools"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "tools" {
  count = var.create_gateway ? 1 : 0

  function_name    = "${var.name_prefix}-tools"
  role             = aws_iam_role.tools[0].arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "index.handler"
  filename         = data.archive_file.tools[0].output_path
  source_code_hash = data.archive_file.tools[0].output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      ANOMALY_TABLE = local.anomaly_table
      # PARAM_PREFIX は渡さない: Neptune は VPC の中で、この Lambda は外にいる（topology は data/ の静的データ）
    }
  }

  depends_on = [aws_cloudwatch_log_group.tools, aws_iam_role_policy.tools]
}

# ---------------------------------------------------------------- gateway role (invokes the Lambda)
data "aws_iam_policy_document" "gateway_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:gateway/${var.name_prefix}-tools-*"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  count = var.create_gateway ? 1 : 0

  name               = "${var.name_prefix}-gateway"
  description        = "AgentCore Gateway role - invokes the tools Lambda"
  assume_role_policy = data.aws_iam_policy_document.gateway_trust.json
}

data "aws_iam_policy_document" "gateway" {
  statement {
    sid       = "InvokeToolsLambda"
    actions   = ["lambda:InvokeFunction"]
    resources = var.create_gateway ? [aws_lambda_function.tools[0].arn] : []
  }
}

resource "aws_iam_role_policy" "gateway" {
  count = var.create_gateway ? 1 : 0

  name   = "${var.name_prefix}-gateway"
  role   = aws_iam_role.gateway[0].name
  policy = data.aws_iam_policy_document.gateway.json
}

resource "aws_bedrockagentcore_gateway" "tools" {
  count = var.create_gateway ? 1 : 0

  name            = "${var.name_prefix}-tools"
  description     = "fukuda-nwc-poc agent tools (MCP)"
  role_arn        = aws_iam_role.gateway[0].arn
  authorizer_type = "AWS_IAM"
  protocol_type   = "MCP"

  protocol_configuration {
    mcp {
      supported_versions = ["2025-06-18"]
    }
  }

  depends_on = [aws_iam_role_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "tools" {
  count = var.create_gateway ? 1 : 0

  name               = "tools"
  description        = "Read only tools backed by the tools Lambda"
  gateway_identifier = aws_bedrockagentcore_gateway.tools[0].gateway_id

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tools[0].arn

        tool_schema {
          dynamic "inline_payload" {
            for_each = local.tools
            content {
              name        = inline_payload.value.name
              description = inline_payload.value.description

              input_schema {
                type = inline_payload.value.inputSchema.type

                dynamic "property" {
                  for_each = inline_payload.value.inputSchema.properties
                  content {
                    name        = property.key
                    type        = property.value.type
                    description = try(property.value.description, null)
                    required    = contains(try(inline_payload.value.inputSchema.required, []), property.key)
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}

# agent/app.py は URL を SSM から引く（環境変数 GATEWAY_URL でも上書きできる）
resource "aws_ssm_parameter" "gateway_url" {
  count = var.create_gateway ? 1 : 0

  name  = "${local.param_prefix}/gateway-url"
  type  = "String"
  value = aws_bedrockagentcore_gateway.tools[0].gateway_url
}
