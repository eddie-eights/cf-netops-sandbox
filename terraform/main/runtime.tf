# ---------------------------------------------------------------- AgentCore Runtime
resource "aws_iam_role" "runtime" {
  name        = "${var.name_prefix}-runtime"
  description = "Execution role for the fukuda-nwc-poc AgentCore Runtime"

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

resource "aws_iam_role_policy" "runtime" {
  name = "runtime"
  role = aws_iam_role.runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrPull"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = "arn:${local.partition}:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}-*"
      },
      {
        Sid      = "EcrToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "LogsGroup"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogStreams", "logs:CreateLogGroup"]
        Resource = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      {
        Sid      = "LogsDescribe"
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:*"
      },
      {
        Sid      = "LogsWrite"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
      },
      {
        Sid      = "Xray"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      },
      {
        Sid      = "Metrics"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" }
        }
      },
      {
        Sid    = "WorkloadToken"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT"]
        Resource = [
          "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
          "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${var.runtime_name}-*",
        ]
      },
      {
        # jp.* の推論プロファイルは東京と大阪のモデルへ振り分けるので、リージョンは * にする
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:${local.partition}:bedrock:*::foundation-model/*",
          "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/*",
        ]
      },
      {
        Sid      = "KbRetrieve"
        Effect   = "Allow"
        Action   = "bedrock:Retrieve"
        Resource = aws_bedrockagent_knowledge_base.kb.arn
      },
      {
        # Standard 階層の判定はプロファイルの行き先リージョンで行われるので、リージョンは * にする
        Sid    = "ApplyGuardrail"
        Effect = "Allow"
        Action = "bedrock:ApplyGuardrail"
        Resource = [
          aws_bedrock_guardrail.this.guardrail_arn,
          "arn:${local.partition}:bedrock:*:${local.account_id}:guardrail-profile/${var.guardrail_profile_id}",
        ]
      },
    ]
  })
}

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name = var.runtime_name
  description        = "fukuda-nwc-poc chat agent"
  role_arn           = aws_iam_role.runtime.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_image_uri
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      subnets         = [aws_subnet.a.id, aws_subnet.b.id]
      security_groups = [aws_security_group.runtime.id]
    }
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  # 放置したセッションを 5 分で畳む（メモリ課金を止める）。1 セッションの寿命は最大 1 時間
  lifecycle_configuration = [{
    idle_runtime_session_timeout = 300
    max_lifetime                 = 3600
  }]

  environment_variables = merge(
    {
      MODEL_ID                   = var.model_id
      BEDROCK_REGION             = var.region
      KNOWLEDGE_BASE_ID          = aws_bedrockagent_knowledge_base.kb.id
      NUMBER_OF_RESULTS          = tostring(var.number_of_results)
      NUMBER_OF_RERANKED_RESULTS = tostring(var.number_of_reranked_results)
      GUARDRAIL_ID               = aws_bedrock_guardrail.this.guardrail_id
      GUARDRAIL_VERSION          = aws_bedrock_guardrail_version.r1.version
      # terraform/stream / terraform/graph が書く SSM（anomaly-table / neptune-endpoint）の接頭辞。無ければ静的データで動く
      PARAM_PREFIX = "/${var.name_prefix}"
    },
    { for k, v in { RERANK_MODEL_ARN = local.rerank_model_arn } : k => v if local.rerank },
  )

  tags = { Name = "${var.name_prefix}-agent" }

  # VPC endpoints ができてから Runtime を作らせる（イメージ取得とログ出力がエンドポイント経由）
  depends_on = [
    aws_iam_role_policy.runtime,
    aws_vpc_endpoint.runtime,
    aws_vpc_endpoint.s3,
    aws_vpc_endpoint.bedrock_agent_runtime,
    aws_vpc_security_group_egress_rule.runtime_https,
    aws_vpc_security_group_ingress_rule.endpoints_from_runtime,
  ]
}
