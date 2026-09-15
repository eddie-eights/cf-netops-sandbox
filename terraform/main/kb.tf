# ---------------------------------------------------------------- knowledge base (S3 -> Titan Embeddings v2 -> OpenSearch Serverless)
# 取り込み元の md を置くバケット。利用者の PC から aws s3 cp で置き、start-ingestion-job で取り込む（README の手順 4）。
# force_destroy = true なので、docs/ web/ lab/ stream/ が残っていても terraform destroy で消える
resource "aws_s3_bucket" "kb" {
  bucket        = "${var.name_prefix}-kb-${local.account_id}"
  force_destroy = true

  tags = { Name = "${var.name_prefix}-kb" }
}

resource "aws_s3_bucket_public_access_block" "kb" {
  bucket                  = aws_s3_bucket.kb.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "kb" {
  bucket = aws_s3_bucket.kb.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "kb" {
  bucket = aws_s3_bucket.kb.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_policy" "kb" {
  bucket = aws_s3_bucket.kb.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.kb.arn, "${aws_s3_bucket.kb.arn}/*"]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.kb]
}

resource "aws_opensearchserverless_security_policy" "kb_encryption" {
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
    Principal = [aws_iam_role.kb.arn, local.kb_admin_principal_arn]
  }])
}

# アクセスポリシーも先に作る。反映に 1 分ほどかかるので、コレクションの作成（数分）の間に効かせてからインデックスを作る
resource "aws_opensearchserverless_collection" "kb" {
  name        = local.collection_name
  type        = "VECTORSEARCH"
  description = "fukuda-nwc-poc knowledge base"
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
  create_duration = "60s"

  depends_on = [aws_opensearchserverless_collection.kb, aws_opensearchserverless_access_policy.kb]
}

# ハイブリッド検索は faiss エンジンと、index が true の text フィールドが要る
resource "opensearch_index" "kb" {
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
}

resource "aws_iam_role" "kb" {
  name        = "${var.name_prefix}-kb"
  description = "Service role for the fukuda-nwc-poc Bedrock knowledge base"

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
  name = "kb"
  role = aws_iam_role.kb.id

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
          Resource = aws_s3_bucket.kb.arn
          Condition = {
            StringEquals = { "aws:ResourceAccount" = local.account_id }
          }
        },
        {
          Sid      = "S3Read"
          Effect   = "Allow"
          Action   = "s3:GetObject"
          Resource = "${aws_s3_bucket.kb.arn}/*"
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
  name        = "${var.name_prefix}-kb"
  description = "fukuda-nwc-poc runbooks (markdown)"
  role_arn    = aws_iam_role.kb.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:${local.partition}:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.kb.arn
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
  knowledge_base_id    = aws_bedrockagent_knowledge_base.kb.id
  name                 = "${var.name_prefix}-docs"
  description          = "Markdown files under s3://<bucket>/docs/"
  data_deletion_policy = "RETAIN"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = aws_s3_bucket.kb.arn
      inclusion_prefixes = ["docs/"]
    }
  }
}

# ---------------------------------------------------------------- guardrail
# Classic 階層は英語・フランス語・スペイン語だけ。日本語を判定させるには Standard 階層にする。
# Standard 階層はクロスリージョン推論が必須で、判定は APAC の他リージョンで行われることがある
resource "aws_bedrock_guardrail" "this" {
  name                      = "${var.name_prefix}-guardrail"
  description               = "fukuda-nwc-poc content filters and prompt attack filter"
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
  description   = "fukuda-nwc-poc r1"
}
