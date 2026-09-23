# ---------------------------------------------------------------- AgentCore Runtime
# 実行ロールそのものは terraform/base/core が持つ（terraform/pipeline/stream / terraform/pipeline/graph がロール名を読んでポリシーを付けるため）。
# ここでは Runtime が動くのに要るポリシーを足し、Runtime を作る
resource "aws_iam_role_policy" "runtime" {
  name = "runtime"
  role = local.runtime_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid      = "EcrPull"
          Effect   = "Allow"
          Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
          Resource = "arn:${local.partition}:ecr:${var.region}:${local.account_id}:repository/${local.name_prefix}-*"
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
            "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${local.runtime_name}-*",
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
      ],
      # KB を作ったときだけ Retrieve を許す
      [for s in [
        {
          Sid      = "KbRetrieve"
          Effect   = "Allow"
          Action   = "bedrock:Retrieve"
          Resource = local.kb ? aws_bedrockagent_knowledge_base.kb[0].arn : ""
        },
      ] : s if local.kb],
      [
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
      ],
    )
  })
}

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name = local.runtime_name
  description        = "${local.name_prefix} chat agent"
  role_arn           = local.runtime_role_arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_image_uri
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      subnets         = local.subnet_ids
      security_groups = [local.runtime_sg_id]
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
      MODEL_ID          = var.model_id
      BEDROCK_REGION    = var.region
      GUARDRAIL_ID      = aws_bedrock_guardrail.this.guardrail_id
      GUARDRAIL_VERSION = aws_bedrock_guardrail_version.r1.version
      # terraform/pipeline/graph が書く SSM（neptune-endpoint。トポロジ・異常・修復案）の接頭辞。無ければ静的データで動く
      PARAM_PREFIX = local.param_prefix
    },
    # KB を作らないときは KNOWLEDGE_BASE_ID を渡さない（agent は Retrieve を飛ばしてモデルとツールだけで答える）
    local.kb ? {
      KNOWLEDGE_BASE_ID          = aws_bedrockagent_knowledge_base.kb[0].id
      NUMBER_OF_RESULTS          = tostring(var.number_of_results)
      NUMBER_OF_RERANKED_RESULTS = tostring(var.number_of_reranked_results)
    } : {},
    { for k, v in { RERANK_MODEL_ARN = local.rerank_model_arn } : k => v if local.kb && local.rerank },
  )

  tags = { Name = "${local.name_prefix}-agent" }

  # VPC endpoints ができてから Runtime を作らせる（イメージ取得とログ出力がエンドポイント経由。ecr / logs は terraform/base/core が先に作ってある）
  depends_on = [
    aws_iam_role_policy.runtime,
    aws_vpc_endpoint.runtime,
    aws_vpc_endpoint.bedrock_agent_runtime,
  ]
}

# ---------------------------------------------------------------- hand the runtime ARN to the chat web
# web EC2 は起動時に環境変数で ARN を受け取るのではなく、この SSM パラメータを読む（60 秒キャッシュ）。
# こうすると agent を後から apply / destroy しても terraform/base/core（web EC2）を作り直さずに済む
resource "aws_ssm_parameter" "runtime_arn" {
  name        = "${local.param_prefix}/runtime-arn"
  description = "ARN of the AgentCore Runtime the chat web invokes (written by terraform/agent)"
  type        = "String"
  value       = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn

  tags = { Name = "${local.name_prefix}-runtime-arn" }
}

resource "aws_iam_role_policy" "web_invoke_runtime" {
  name = "invoke-runtime"
  role = local.web_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "bedrock-agentcore:InvokeAgentRuntime"
      Resource = [
        aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn,
        "${aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn}/*",
      ]
    }]
  })
}
