# ---------------------------------------------------------------- VPC endpoints
# 残すのは S3 の gateway エンドポイントと、OpenSearch Serverless の VPC エンドポイント（create_opensearch_endpoint のとき）だけ。
# S3 の gateway エンドポイントは無料（S3 / S3 Tables / ECR のレイヤー / AL2023 の dnf リポジトリの転送量を NAT の 0.062 USD/GB から外す）。
# S3 のエンドポイントポリシーは付けない: バケットへの操作の絞り込みは各ロールの IAM ポリシーで行う。
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

# OpenSearch Serverless のコレクション（terraform/agent の KB、terraform/pipeline/analytics の logs）はどちらも公開せず、
# ネットワークポリシーの SourceVPCEs にこのエンドポイントだけを書く。NAT Gateway から出ると公開扱いになり、
# ネットワークポリシーには IP の許可リストが無いので、VPC の中から閉じて届く経路はこれしかない。
# 1 つの VPC に 1 本あれば全コレクションに届き（AWS の文書「You only need one OpenSearch Serverless VPC endpoint in a VPC」）、
# 作ると AOSS が *.<region>.aoss.amazonaws.com の private hosted zone を VPC に付ける。2 本目を作らないよう、ここに 1 本だけ置く。
# ops/up.sh は CREATE_KB か SINK_OPENSEARCH のとき create_opensearch_endpoint=true で apply する。
# ENI は 2 AZ（1 本 1.4 セント/h × 2）。SG は endpoints（internal からの 443 だけ）
resource "aws_opensearchserverless_vpc_endpoint" "aoss" {
  count = var.create_opensearch_endpoint ? 1 : 0

  name               = "${local.name_prefix}-aoss"
  vpc_id             = aws_vpc.this.id
  subnet_ids         = [aws_subnet.a.id, aws_subnet.b.id]
  security_group_ids = [aws_security_group.endpoints.id]
}
