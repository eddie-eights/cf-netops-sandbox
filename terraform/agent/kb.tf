# ---------------------------------------------------------------- knowledge base (S3 -> Titan Embeddings v2 -> OpenSearch Serverless)
# create_knowledge_base = true のときだけ作る（count）。バケットは terraform/base/core のもの（web/ lab/ stream/ と共用）。
# 取り込み元の md は利用者の PC から aws s3 cp で docs/ に置き、start-ingestion-job で取り込む（ops/up.sh の手順 4）
resource "aws_opensearchserverless_security_policy" "kb_encryption" {
  count = local.kb ? 1 : 0

  name        = local.collection_name
  type        = "encryption"
  description = "AWS owned key for the knowledge base collection"

  policy = jsonencode({
    Rules = [{
      ResourceType = "collection"
      Resource     = ["collection/${local.collection_name}"]
    }]
    AWSOwnedKey = true
  })
}

# 公開しない。届くのは土台（terraform/base/core）の VPC エンドポイントと Bedrock のサービス側だけ。
# Runtime はコレクションを直接呼ばない（Retrieve を呼ぶと Bedrock が SourceServices の経路でサービス側から検索する）。
# 取り込み（start-ingestion-job）も Bedrock がこの経路で書く。索引は VPC の中の Lambda（下の kb_index）が作る
resource "aws_opensearchserverless_security_policy" "kb_network" {
  count = local.kb ? 1 : 0

  name        = local.collection_name
  type        = "network"
  description = "Collection reachable only through the OpenSearch Serverless VPC endpoint of terraform/base/core and from Bedrock"

  policy = jsonencode([{
    Rules = [{
      ResourceType = "collection"
      Resource     = ["collection/${local.collection_name}"]
    }]
    AllowFromPublic = false
    SourceVPCEs     = [local.aoss_vpce_id]
    SourceServices  = ["bedrock.amazonaws.com"]
  }])

  lifecycle {
    precondition {
      condition     = local.aoss_vpce_id != ""
      error_message = "terraform/base/core に OpenSearch Serverless の VPC エンドポイントが無い。terraform/base/core を -var create_opensearch_endpoint=true で apply し直す（ops/up.sh は CREATE_KB=1 のとき付ける）。"
    }
  }
}

# KB のサービスロールは索引の読み書き、索引を作る Lambda は CreateIndex / DescribeIndex だけ。人（apply した人）の ARN は入れない
resource "aws_opensearchserverless_access_policy" "kb" {
  count = local.kb ? 1 : 0

  name        = local.collection_name
  type        = "data"
  description = "Knowledge base service role reads and writes, the index Lambda creates the vector index"

  policy = jsonencode([
    {
      Rules = [
        {
          ResourceType = "collection"
          Resource     = ["collection/${local.collection_name}"]
          Permission   = ["aoss:CreateCollectionItems", "aoss:DescribeCollectionItems", "aoss:UpdateCollectionItems"]
        },
        {
          ResourceType = "index"
          Resource     = ["index/${local.collection_name}/*"]
          Permission   = ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:UpdateIndex", "aoss:DeleteIndex", "aoss:ReadDocument", "aoss:WriteDocument"]
        },
      ]
      Principal = [aws_iam_role.kb[0].arn]
    },
    {
      Rules = [{
        ResourceType = "index"
        Resource     = ["index/${local.collection_name}/${local.index_name}"]
        Permission   = ["aoss:CreateIndex", "aoss:DescribeIndex"]
      }]
      Principal = [aws_iam_role.kb_index[0].arn]
    },
  ])
}

# アクセスポリシーも先に作る。反映に 1 分ほどかかるので、コレクションの作成（数分）の間に効かせてからインデックスを作る
resource "aws_opensearchserverless_collection" "kb" {
  count = local.kb ? 1 : 0

  name        = local.collection_name
  type        = "VECTORSEARCH"
  description = "${local.name_prefix} knowledge base"
  # スタンバイを切ると最小 OCU が半分になる（検証用。可用性は下がる）
  standby_replicas = "DISABLED"

  tags = { Name = local.collection_name }

  depends_on = [
    aws_opensearchserverless_security_policy.kb_encryption,
    aws_opensearchserverless_security_policy.kb_network,
    aws_opensearchserverless_access_policy.kb,
  ]
}

# コレクションが ACTIVE になってもデータアクセスポリシーとエンドポイントの DNS が効くまで少し掛かる
resource "time_sleep" "kb_collection_ready" {
  count = local.kb ? 1 : 0

  create_duration = "60s"

  depends_on = [aws_opensearchserverless_collection.kb, aws_opensearchserverless_access_policy.kb]
}

# ---------------------------------------------------------------- vector index (Lambda in the VPC)
# コレクションは VPC エンドポイントからしか届かないので、Terraform を打つ PC からは索引を作れない。
# VPC の中（土台の internal SG。endpoints SG が 443 を受ける）の Lambda（agent/kb_index.py）を apply のときに 1 回呼んで作る。
# 索引がもうあれば作らない（mappings が違っても直さない）。mappings を変えるときは
# -replace=aws_opensearchserverless_collection.kb[0] でコレクションごと作り直し、そのあと取り込みをやり直す
data "archive_file" "kb_index" {
  type        = "zip"
  output_path = "${path.module}/.build/kb_index.zip"

  source {
    content  = file("${path.module}/../../agent/kb_index.py")
    filename = "index.py"
  }
}

resource "aws_iam_role" "kb_index" {
  count = local.kb ? 1 : 0

  name        = "${local.name_prefix}-kb-index"
  description = "Lambda that creates the vector index of the ${local.name_prefix} knowledge base from inside the VPC"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name_prefix}-kb-index" }
}

resource "aws_iam_role_policy" "kb_index" {
  count = local.kb ? 1 : 0

  name = "kb-index"
  role = aws_iam_role.kb_index[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.kb_index[0].arn}:*"
      },
      {
        # VPC の中で動くので ENI を作る（AWSLambdaVPCAccessExecutionRole と同じ中身。マネージドポリシーは付けない）
        Sid      = "VpcEni"
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]
        Resource = "*"
      },
      {
        # 中身に触れるかはデータアクセスポリシー（CreateIndex / DescribeIndex だけ）が決める
        Sid      = "OpenSearch"
        Effect   = "Allow"
        Action   = "aoss:APIAccessAll"
        Resource = aws_opensearchserverless_collection.kb[0].arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "kb_index" {
  count = local.kb ? 1 : 0

  name              = "/aws/lambda/${local.name_prefix}-kb-index"
  retention_in_days = 7
}

resource "aws_lambda_function" "kb_index" {
  count = local.kb ? 1 : 0

  function_name    = "${local.name_prefix}-kb-index"
  description      = "Creates the vector index of the knowledge base collection (called once by terraform apply)"
  role             = aws_iam_role.kb_index[0].arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "index.handler"
  filename         = data.archive_file.kb_index.output_path
  source_code_hash = data.archive_file.kb_index.output_base64sha256
  # データアクセスポリシーの反映待ち（403）を中で打ち直すので長めにとる
  timeout     = 300
  memory_size = 128

  vpc_config {
    subnet_ids         = local.subnet_ids
    security_group_ids = [local.runtime_sg_id]
  }

  depends_on = [aws_cloudwatch_log_group.kb_index, aws_iam_role_policy.kb_index]
}

# ハイブリッド検索は faiss エンジンと、index が true の text フィールドが要る。
# 取り込みのあと Bedrock が id / x-amz-bedrock-kb-* のフィールドを索引に足すが、Lambda は索引があれば触らないので置き換えにならない
# （2026-09-17 までの opensearch provider では、その差分で毎回 -/+ になって KB のベクトルが消えた）
resource "aws_lambda_invocation" "kb_index" {
  count = local.kb ? 1 : 0

  function_name = aws_lambda_function.kb_index[0].function_name

  input = jsonencode({
    endpoint = aws_opensearchserverless_collection.kb[0].collection_endpoint
    index    = local.index_name
    body = {
      settings = { index = { knn = true } }
      mappings = {
        properties = {
          "bedrock-kb-vector" = {
            type      = "knn_vector"
            dimension = 1024
            method = {
              engine     = "faiss"
              name       = "hnsw"
              space_type = "l2"
              parameters = {
                ef_construction = 128
                m               = 24
              }
            }
          }
          AMAZON_BEDROCK_TEXT_CHUNK = {
            type  = "text"
            index = true
          }
          AMAZON_BEDROCK_METADATA = {
            type  = "text"
            index = false
          }
        }
      }
    }
  })

  depends_on = [time_sleep.kb_collection_ready]
}

resource "aws_iam_role" "kb" {
  count = local.kb ? 1 : 0

  name        = "${local.name_prefix}-kb"
  description = "Service role for the ${local.name_prefix} Bedrock knowledge base"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:knowledge-base/*" }
      }
    }]
  })

  tags = { Name = "${local.name_prefix}-kb" }
}

locals {
  # Retrieve のリランクは呼び出し側ではなく KB のサービスロールの権限で動く
  kb_rerank_statements = [for s in [
    {
      Sid      = "Rerank"
      Effect   = "Allow"
      Action   = "bedrock:Rerank"
      Resource = "*"
    },
    {
      Sid      = "RerankModel"
      Effect   = "Allow"
      Action   = "bedrock:InvokeModel"
      Resource = local.rerank_model_arn
    },
  ] : s if local.rerank]
}

resource "aws_iam_role_policy" "kb" {
  count = local.kb ? 1 : 0

  name = "kb"
  role = aws_iam_role.kb[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid      = "ListModels"
          Effect   = "Allow"
          Action   = ["bedrock:ListFoundationModels", "bedrock:ListCustomModels"]
          Resource = "*"
        },
        {
          Sid      = "Embedding"
          Effect   = "Allow"
          Action   = "bedrock:InvokeModel"
          Resource = "arn:${local.partition}:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"
        },
      ],
      local.kb_rerank_statements,
      [
        {
          Sid      = "S3List"
          Effect   = "Allow"
          Action   = "s3:ListBucket"
          Resource = local.bucket_arn
          Condition = {
            StringEquals = { "aws:ResourceAccount" = local.account_id }
          }
        },
        {
          Sid      = "S3Read"
          Effect   = "Allow"
          Action   = "s3:GetObject"
          Resource = "${local.bucket_arn}/*"
          Condition = {
            StringEquals = { "aws:ResourceAccount" = local.account_id }
          }
        },
        {
          # コレクションの ARN は名前でなく ID で決まり、参照するとアクセスポリシーと循環するのでアカウント内に絞る。
          # 中身に触れるかはデータアクセスポリシー（aws_opensearchserverless_access_policy.kb）が決める
          Sid      = "OpenSearch"
          Effect   = "Allow"
          Action   = "aoss:APIAccessAll"
          Resource = "arn:${local.partition}:aoss:${var.region}:${local.account_id}:collection/*"
        },
      ],
    )
  })
}

resource "aws_bedrockagent_knowledge_base" "kb" {
  count = local.kb ? 1 : 0

  name        = "${local.name_prefix}-kb"
  description = "${local.name_prefix} runbooks (markdown)"
  role_arn    = aws_iam_role.kb[0].arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:${local.partition}:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.kb[0].arn
      vector_index_name = local.index_name
      field_mapping {
        vector_field   = "bedrock-kb-vector"
        text_field     = "AMAZON_BEDROCK_TEXT_CHUNK"
        metadata_field = "AMAZON_BEDROCK_METADATA"
      }
    }
  }

  tags = { Name = "${local.name_prefix}-kb" }

  depends_on = [aws_lambda_invocation.kb_index, aws_iam_role_policy.kb]
}

# DELETE だと destroy 時にベクトルの削除が走り、コレクションが先に消えると失敗する。RETAIN にしてコレクションごと消す
resource "aws_bedrockagent_data_source" "docs" {
  count = local.kb ? 1 : 0

  knowledge_base_id    = aws_bedrockagent_knowledge_base.kb[0].id
  name                 = "${local.name_prefix}-docs"
  description          = "Markdown files under s3://<bucket>/docs/"
  data_deletion_policy = "RETAIN"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = local.bucket_arn
      inclusion_prefixes = ["docs/"]
    }
  }
}

# ---------------------------------------------------------------- guardrail
# Classic 階層は英語・フランス語・スペイン語だけ。日本語を判定させるには Standard 階層にする。
# Standard 階層はクロスリージョン推論が必須で、判定は APAC の他リージョンで行われることがある
resource "aws_bedrock_guardrail" "this" {
  name                      = "${local.name_prefix}-guardrail"
  description               = "${local.name_prefix} content filters and prompt attack filter"
  blocked_input_messaging   = "この質問にはお答えできません。業務に関する内容で聞き直してください。"
  blocked_outputs_messaging = "回答にガードレールで止める内容が含まれたため、表示しません。聞き方を変えてください。"

  cross_region_config {
    guardrail_profile_identifier = "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:guardrail-profile/${var.guardrail_profile_id}"
  }

  content_policy_config {
    tier_config = [{ tier_name = "STANDARD" }]

    # ネットワーク運用の語（攻撃・遮断・kill など）で誤検知しにくいよう MEDIUM にする
    filters_config {
      type            = "HATE"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }
    filters_config {
      type            = "INSULTS"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }
    filters_config {
      type            = "SEXUAL"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }
    filters_config {
      type            = "VIOLENCE"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }
    filters_config {
      type            = "MISCONDUCT"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }
    # プロンプト攻撃は入力だけに効く。出力側は NONE にする決まり
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }

  tags = { Name = "${local.name_prefix}-guardrail" }
}

# 版は作成時点のガードレールを固定する。ガードレールを変えたら description の r1 を r2 に上げて、版を作り直させる
resource "aws_bedrock_guardrail_version" "r1" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "${local.name_prefix} r1"
}
