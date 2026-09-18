# ---------------------------------------------------------------- AgentCore Gateway (MCP) + tools Lambda
# The chat runtime (agent/app.py) lists the tools through the gateway URL (SSM <prefix>/gateway-url) and calls them over MCP
# instead of its built-in functions. The Lambda runs the same agent/topology.py, agent/anomalies.py, agent/evidence.py and
# agent/proposals.py inside the VPC (subnet a), so it reads Neptune (terraform/pipeline/graph), the logs collection and the metrics
# workspace (terraform/pipeline/analytics), the anomaly table (terraform/pipeline/stream) and the proposal table (proposals.tf).
# Without graph / analytics the topology comes from data/ and the evidence tools say so.

locals {
  tools = jsondecode(file("${path.module}/../../tools/tools.json"))

  # Lambda の zip に入れるファイル（プロジェクトの中の場所 = zip の中の名前）。
  # handler.py だけ名前が変わる（Lambda のハンドラが index.handler）。proposals.py は読むだけで、承認・却下はツールに出していない。
  # agent/ のモジュールを増やしたらここにも足す（同じ一覧が agent/Dockerfile と terraform/base/core の upload_web_command にもある）
  tools_files = {
    "../../tools/handler.py"         = "index.py"
    "../../agent/toolkit.py"         = "toolkit.py"
    "../../agent/topology.py"        = "topology.py"
    "../../agent/anomalies.py"       = "anomalies.py"
    "../../agent/graph.py"           = "graph.py"
    "../../agent/evidence.py"        = "evidence.py"
    "../../agent/proposals.py"       = "proposals.py"
    "../../agent/data/topology.json" = "data/topology.json"
  }
}

data "archive_file" "tools" {
  count = var.create_gateway ? 1 : 0

  type        = "zip"
  output_path = "${path.module}/.build/tools.zip"

  dynamic "source" {
    for_each = local.tools_files

    content {
      content  = file("${path.module}/${source.key}")
      filename = source.value
    }
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

  name               = "${local.name_prefix}-tools"
  description        = "Tools Lambda behind the MCP gateway - reads the anomaly and proposal tables, Neptune, the logs collection and the metrics workspace"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "tools" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${local.name_prefix}-tools:*"]
  }

  statement {
    sid       = "Anomalies"
    actions   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"]
    resources = [local.anomaly_table_arn, "${local.anomaly_table_arn}/index/*"]
  }

  # 修復案は読むだけ。UpdateItem は付けない（承認・却下は web ロールだけが書ける。proposals.tf の decide_access）
  statement {
    sid       = "ProposalsRead"
    actions   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"]
    resources = local.proposal_table_arns
  }

  # VPC の中で動くので ENI を作る（AWSLambdaVPCAccessExecutionRole と同じ中身。マネージドポリシーは付けない）
  statement {
    sid       = "VpcEni"
    actions   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]
    resources = ["*"]
  }

  # Neptune のエンドポイント（terraform/pipeline/graph）と異常テーブル名（terraform/pipeline/stream）を SSM から引く
  statement {
    sid       = "Parameters"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${local.param_prefix}/*"]
  }

  dynamic "statement" {
    for_each = local.neptune_data_arn != "" ? [1] : []
    content {
      sid       = "NeptuneRead"
      actions   = ["neptune-db:ReadDataViaQuery", "neptune-db:GetQueryStatus"]
      resources = [local.neptune_data_arn]
    }
  }

  dynamic "statement" {
    for_each = local.opensearch_collection_arn != "" ? [1] : []
    content {
      sid       = "LogsCollection"
      actions   = ["aoss:APIAccessAll"]
      resources = [local.opensearch_collection_arn]
    }
  }

  dynamic "statement" {
    for_each = local.prometheus_workspace_arn != "" ? [1] : []
    content {
      sid       = "MetricsQuery"
      actions   = ["aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"]
      resources = [local.prometheus_workspace_arn]
    }
  }
}

# ---------------------------------------------------------------- tools Lambda network (subnet a of terraform/base/core)
resource "aws_security_group" "tools" {
  count = var.create_gateway ? 1 : 0

  name        = "${local.name_prefix}-tools"
  description = "Tools Lambda - HTTPS to the VPC endpoints (DynamoDB gateway, SSM, aoss, aps, logs) and 8182 to Neptune"
  vpc_id      = local.vpc_id

  tags = { Name = "${local.name_prefix}-tools" }
}

resource "aws_vpc_security_group_egress_rule" "tools_https" {
  count = var.create_gateway ? 1 : 0

  security_group_id = aws_security_group.tools[0].id
  description       = "DynamoDB gateway, SSM / aoss / aps / logs endpoints"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "tools_neptune" {
  count = var.create_gateway && local.neptune_sg_id != "" ? 1 : 0

  security_group_id            = aws_security_group.tools[0].id
  description                  = "Gremlin to Neptune (terraform/pipeline/graph)"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = local.neptune_sg_id
}

resource "aws_vpc_security_group_ingress_rule" "neptune_from_tools" {
  count = var.create_gateway && local.neptune_sg_id != "" ? 1 : 0

  security_group_id            = local.neptune_sg_id
  description                  = "Tools Lambda of terraform/workflow"
  ip_protocol                  = "tcp"
  from_port                    = 8182
  to_port                      = 8182
  referenced_security_group_id = aws_security_group.tools[0].id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_tools" {
  count = var.create_gateway ? 1 : 0

  security_group_id            = local.endpoint_sg_id
  description                  = "Tools Lambda through the endpoints of terraform/base/core and terraform/pipeline/analytics (aoss, aps)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.tools[0].id
}

# 検索だけ。terraform/pipeline/analytics の data access policy は Spark の実行ロール（書く側）だけなので、読む側はここで足す
resource "aws_opensearchserverless_access_policy" "tools" {
  count = var.create_gateway && local.opensearch_collection_name != "" ? 1 : 0

  name        = "${local.name_prefix}-logs-read"
  type        = "data"
  description = "Tools Lambda and chat runtime read the logs collection"

  policy = jsonencode([{
    Rules = [
      {
        ResourceType = "collection"
        Resource     = ["collection/${local.opensearch_collection_name}"]
        Permission   = ["aoss:DescribeCollectionItems"]
      },
      {
        ResourceType = "index"
        Resource     = ["index/${local.opensearch_collection_name}/*"]
        Permission   = ["aoss:DescribeIndex", "aoss:ReadDocument"]
      },
    ]
    Principal = [aws_iam_role.tools[0].arn]
  }])
}

resource "aws_iam_role_policy" "tools" {
  count = var.create_gateway ? 1 : 0

  name   = "${local.name_prefix}-tools"
  role   = aws_iam_role.tools[0].name
  policy = data.aws_iam_policy_document.tools.json
}

resource "aws_cloudwatch_log_group" "tools" {
  count = var.create_gateway ? 1 : 0

  name              = "/aws/lambda/${local.name_prefix}-tools"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "tools" {
  count = var.create_gateway ? 1 : 0

  function_name    = "${local.name_prefix}-tools"
  role             = aws_iam_role.tools[0].arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "index.handler"
  filename         = data.archive_file.tools[0].output_path
  source_code_hash = data.archive_file.tools[0].output_base64sha256
  timeout          = 60
  memory_size      = 256

  # VPC の中（サブネット a）。Neptune / aoss / aps / ssm のエンドポイントは全部このサブネットから届く。NAT が無いので外には出ない
  vpc_config {
    subnet_ids         = [local.subnet_id]
    security_group_ids = [aws_security_group.tools[0].id]
  }

  environment {
    variables = {
      PARAM_PREFIX         = local.param_prefix # graph.py が <prefix>/neptune-endpoint を引く（無ければ data/ の静的トポロジ）
      ANOMALY_TABLE        = local.anomaly_table
      PROPOSAL_TABLE       = aws_dynamodb_table.proposals.name
      OPENSEARCH_ENDPOINT  = local.opensearch_endpoint
      OPENSEARCH_INDEX     = local.opensearch_index
      PROMETHEUS_QUERY_URL = local.prometheus_query_url
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
      values   = ["arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:gateway/${local.name_prefix}-tools-*"]
    }
  }
}

resource "aws_iam_role" "gateway" {
  count = var.create_gateway ? 1 : 0

  name               = "${local.name_prefix}-gateway"
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

  name   = "${local.name_prefix}-gateway"
  role   = aws_iam_role.gateway[0].name
  policy = data.aws_iam_policy_document.gateway.json
}

resource "aws_bedrockagentcore_gateway" "tools" {
  count = var.create_gateway ? 1 : 0

  name            = "${local.name_prefix}-tools"
  description     = "${local.name_prefix} agent tools (MCP)"
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
  description        = "Read only tools backed by the tools Lambda (topology, anomalies, proposals, logs, metrics)"
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
