# ---------------------------------------------------------------- VPC endpoints
# 残すのは S3 の gateway エンドポイントだけ（無料。S3 / S3 Tables / ECR のレイヤー / AL2023 の dnf リポジトリの転送量を NAT の 0.062 USD/GB から外す）。
# エンドポイントポリシーは付けない: バケットへの操作の絞り込みは各ロールの IAM ポリシーで行う。
# ポリシーで絞っていた頃は 2026-09-15（ListBucket が落ちて web/ の取得が失敗）と 2026-09-17（S3 Tables の metadata.json が AccessDenied）に止まった。
# ssm / ssmmessages / ecr.api / ecr.dkr / logs / bedrock-* / s3tables / events / aps / sqs のインターフェース型エンドポイントは
# 2026-09-26 に NAT Gateway（vpc.tf）に置き換えた。戻すときは 7c42b0f の terraform/ を見る
resource "aws_vpc_endpoint" "s3" {
  count = var.create_s3_gateway_endpoint ? 1 : 0

  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name_prefix}-s3" }
}
