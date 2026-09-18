# ---------------------------------------------------------------- VPC endpoints
# ここに置くのは S3 gateway と ssm / ssmmessages と、機能をまたいで使う ecr.api / ecr.dkr / logs（shared）。
# Runtime だけが使う bedrock-runtime（2 AZ）と bedrock-agentcore（web が Runtime を呼ぶ）は terraform/agent が作る
locals {
  ssm_endpoint_sg_ids = concat([aws_security_group.endpoints.id], aws_security_group.client[*].id)
}

# VPC の中から S3 に出る経路はこれだけ。許すのは 4 つ: この root module のバケット（EC2 が web/ を取る、lab が lab/ を取る、
# MSK Connect が stream/ に書き、Spark が analytics/ を読み書きする）、ECR のレイヤー置き場（Runtime のイメージ取得）、
# AL2023 の dnf リポジトリ（EC2 に python3.13 を入れる）、S3 Tables のデータ置き場（terraform/pipeline/analytics の Spark が parquet を書く。
# S3 Tables のテーブルバケットのデータは `<uuid>--table-s3` という名前の S3 バケットに置かれ、S3 の API で読み書きする。2026-09-17 確認）。
# バケットへの操作の絞り込みは各ロールの IAM ポリシーで行う（ここで絞ると 2026-09-15 のように ListBucket が落ちて web/ の取得が失敗する）。
# VPC ごとこの root module のものなので、他のワークロードには影響しない
resource "aws_vpc_endpoint" "s3" {
  count = var.create_s3_gateway_endpoint ? 1 : 0

  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowKbBucket"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.kb.arn, "${aws_s3_bucket.kb.arn}/*"]
      },
      {
        Sid       = "AllowECRLayerAccess"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "arn:${local.partition}:s3:::prod-${var.region}-starport-layer-bucket/*"
      },
      {
        Sid       = "AllowAL2023Repos"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "arn:${local.partition}:s3:::al2023-repos-${var.region}-de612dc2/*"
      },
      # S3 Tables のデータ・メタデータ（terraform/pipeline/analytics の Spark が Iceberg の S3FileIO で読み書きする *--table-s3 バケット）。
      # オブジェクトの要求でも IAM が評価するのは s3:* ではなく s3tables:GetTableData / PutTableData とテーブルの ARN なので、
      # s3:* + arn:aws:s3:::*--table-s3 だけだとこのエンドポイントで 403 になる（2026-09-17 に Spark のジョブが metadata.json の読みで AccessDenied。
      # ロール側は IAM シミュレータで allowed だった）。念のため s3 の形も残す
      {
        Sid       = "AllowS3TablesData"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3tables:*"
        Resource = [
          "arn:${local.partition}:s3tables:${var.region}:${local.account_id}:bucket/*",
          "arn:${local.partition}:s3tables:${var.region}:${local.account_id}:bucket/*/table/*",
        ]
      },
      {
        Sid       = "AllowS3TablesObjects"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:*"
        Resource  = ["arn:${local.partition}:s3:::*--table-s3", "arn:${local.partition}:s3:::*--table-s3/*"]
      },
    ]
  })

  tags = { Name = "${local.name_prefix}-s3" }
}

# ---------------------------------------------------------------- VPC endpoints for the chat web EC2 (1 AZ)
resource "aws_vpc_endpoint" "ssm" {
  for_each = var.create_ssm_endpoints ? toset(["ssm", "ssmmessages"]) : toset([])

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.a.id]
  security_group_ids  = local.ssm_endpoint_sg_ids

  tags = { Name = "${local.name_prefix}-${each.value}" }
}

# ---------------------------------------------------------------- VPC endpoints shared by the features (2 AZ)
# ecr.api / ecr.dkr / logs は 1 つの機能のものではない: Runtime（agent）と lab の EC2 と Fargate のワーカー（workflow）が ECR からイメージを取り、
# Runtime と Spark のジョブ（analytics）と MSK Connect（stream）が CloudWatch Logs に書く。2026-09-17 まで terraform/agent が持っていて、
# AGENT=0 PIPELINE=1 だと lab の docker pull と Spark のログ出力が届かずに落ちた（同日に実測）。Runtime の ENI が 2 AZ に置かれるので 2 AZ に置く。
# 同じ VPC に private DNS 付きの同じサービスのエンドポイントは 2 本作れないので、他のルートでは作らない
resource "aws_vpc_endpoint" "shared" {
  for_each = var.create_shared_endpoints ? {
    "ecr-api" = "ecr.api"
    "ecr-dkr" = "ecr.dkr"
    "logs"    = "logs"
  } : {}

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.a.id, aws_subnet.b.id]
  security_group_ids  = [aws_security_group.endpoints.id]

  tags = { Name = "${local.name_prefix}-${each.key}" }
}
