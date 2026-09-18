# ---------------------------------------------------------------- knowledge base (S3 -> Titan Embeddings v2 -> OpenSearch Serverless)
# create_knowledge_base = true のときだけ作る（count）。バケットは terraform/base/core のもの（web/ lab/ stream/ と共用）。
# 取り込み元の md は利用者の PC から aws s3 cp で docs/ に置き、start-ingestion-job で取り込む（docs/deploy-manual.md の手順 4）
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

# Runtime はコレクションを直接呼ばない（Retrieve を呼ぶと Bedrock がサービス側から検索する）。
# 非公開（SourceVPCEs / SourceServices）にすると PC からのインデックス作成が通らないので、公開にしてデータアクセスポリシーで絞る
resource "aws_opensearchserverless_security_policy" "kb_network" {
  count = local.kb ? 1 : 0

  name        = local.collection_name
  type        = "network"
  description = "Collection endpoint reachable over the public AWS endpoint, data access limited by the access policy"

  policy = jsonencode([{
    Rules = [{
      ResourceType = "collection"
      Resource     = ["collection/${local.collection_name}"]
    }]
    AllowFromPublic = true
  }])
}

resource "aws_opensearchserverless_access_policy" "kb" {
  count = local.kb ? 1 : 0

  name        = local.collection_name
  type        = "data"
  description = "Knowledge base service role and the deployer only"

  policy = jsonencode([{
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
    Principal = [aws_iam_role.kb[0].arn, local.kb_admin_principal_arn]
  }])
}

# アクセスポリシーも先に作る。反映に 1 分ほどかかるので、コレクションの作成（数分）の間に効かせてからインデックスを作る
resource "aws_opensearchserverless_collection" "kb" {
  count = local.kb ? 1 : 0

  name        = local.collection_name
  type        = "VECTORSEARCH"
  description = "${var.name_prefix} knowledge base"
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

# ハイブリッド検索は faiss エンジンと、index が true の text フィールドが要る
resource "opensearch_index" "kb" {
  count = local.kb ? 1 : 0

  name          = local.index_name
  index_knn     = true
  force_destroy = true

  mappings = jsonencode({
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
  })

  depends_on = [time_sleep.kb_collection_ready]

  # 取り込みのあと Bedrock が id / x-amz-bedrock-kb-* のフィールドを索引に足し、provider は既定値の index = true を返さない。
  # どちらも mappings の差分になって毎回 -/+（置き換え）になり、KB のベクトルが消える（2026-09-17 に Mac の 2 回目の ops/up.sh で実測）。
  # mappings を変えたいときは -replace=opensearch_index.kb[0] を付けて手で置き換え、そのあと取り込みをやり直す。
  lifecycle {
    ignore_changes = [mappings]
  }
}

resource "aws_iam_role" "kb" {
  count = local.kb ? 1 : 0

  name        = "${var.name_prefix}-kb"
  description = "Service role for the ${var.name_prefix} Bedrock knowledge base"

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

  tags = { Name = "${var.name_prefix}-kb" }
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

  name        = "${var.name_prefix}-kb"
  description = "${var.name_prefix} runbooks (markdown)"
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

  tags = { Name = "${var.name_prefix}-kb" }

  depends_on = [opensearch_index.kb, aws_iam_role_policy.kb]
}

# DELETE だと destroy 時にベクトルの削除が走り、コレクションが先に消えると失敗する。RETAIN にしてコレクションごと消す
resource "aws_bedrockagent_data_source" "docs" {
  count = local.kb ? 1 : 0

  knowledge_base_id    = aws_bedrockagent_knowledge_base.kb[0].id
  name                 = "${var.name_prefix}-docs"
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
  name                      = "${var.name_prefix}-guardrail"
  description               = "${var.name_prefix} content filters and prompt attack filter"
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

  tags = { Name = "${var.name_prefix}-guardrail" }
}

# 版は作成時点のガードレールを固定する。ガードレールを変えたら description の r1 を r2 に上げて、版を作り直させる
resource "aws_bedrock_guardrail_version" "r1" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "${var.name_prefix} r1"
}
