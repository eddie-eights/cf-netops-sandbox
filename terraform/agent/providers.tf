provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = var.name_prefix
      owner   = var.owner
    }
  }
}

# コレクションのエンドポイントは apply の途中で決まる。healthcheck = false にして、最初のリソース操作まで接続しない。
# 署名はこの apply を打った認証情報（= kb_admin_principal_arn がデータアクセスポリシーに載っている本人）で行う。
# create_knowledge_base = false のときはこの provider を使うリソースが無い。url は必須なので、つなぎに行かないダミーを入れる
provider "opensearch" {
  url                   = local.kb ? aws_opensearchserverless_collection.kb[0].collection_endpoint : "https://localhost"
  aws_region            = var.region
  healthcheck           = false
  sign_aws_requests     = true
  aws_signature_service = "aoss"
  # 社内の SSL 検査で証明書チェーンが社内 CA に置き換わる PC では、その CA の PEM を渡す（README「社内 PC で使うとき」）
  cacert_file = var.opensearch_cacert_file != "" ? var.opensearch_cacert_file : null
}
