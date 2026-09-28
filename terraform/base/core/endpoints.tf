# ---------------------------------------------------------------- VPC endpoints
# AWS の API へは全部 VPC エンドポイントから行く。VPC にはほかに出口が無い（NAT Gateway は vpc.tf の create_nat_gateway のときだけ、Splunk の HEC のために作る）。
# 経路を VPC エンドポイントに寄せると、リクエストに aws:SourceVpc（この VPC）が付くので、perimeter.tf の拒否（IAM とリソースポリシー）で
# 「この VPC の外からの呼び出し」を止められる。NAT から出た呼び出しには aws:SourceVpc が付かない（2026-09-28 ユーザー決定: 閉域で高セキュアに。同日、NAT も既定では作らないと決めた）。
# エンドポイントが無いサービスは、NAT が無いと接続のタイムアウトになる（docs/troubleshooting.md）。
#
# S3 の gateway エンドポイントは無料（S3 / S3 Tables のデータ / ECR のレイヤー / AL2023 の dnf リポジトリ）。
# S3 のエンドポイントポリシーは付けない: dnf のリポジトリは匿名の GET で、ECR のレイヤーは ECR が署名した URL なので、
# aws:PrincipalAccount やバケットで絞ると止まる。ポリシーで絞っていた頃は 2026-09-15（ListBucket）と 2026-09-17（S3 Tables の metadata.json）に止まった。
# バケットの側（bucket.tf、terraform/pipeline/analytics の S3 Tables）で aws:SourceVpc を要求する
resource "aws_vpc_endpoint" "s3" {
  count = var.create_s3_gateway_endpoint ? 1 : 0

  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name_prefix}-s3" }
}

# OpenSearch Serverless のコレクション（terraform/agent の KB、terraform/pipeline/analytics の logs）はどちらも公開せず、
# ネットワークポリシーの SourceVPCEs にこのエンドポイントだけを書く。NAT Gateway から出ると（NAT があっても）公開扱いになり、
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

# インターフェース型エンドポイント。どれを作るかは ops/up.sh が機能から決めて var.interface_endpoints で渡す
# （土台は ssm / ssmmessages: Web の EC2 の SSM Agent とポートフォワーディング、SSM パラメータ）。
# 同じサービスの private DNS 付きエンドポイントは 1 つの VPC に 1 本しか作れないので、ルートごとに持たずここに集める
# （2026-09-26 まではルートごとに持っていた。7c42b0f）。
# エンドポイントポリシーは「このアカウントのプリンシパルだけ」: 盗んだ他のアカウントの鍵でこの VPC から外へ持ち出す経路を塞ぐ。
# 1 本 1.4 セント/h × AZ（endpoints_multi_az = false ならサブネット a だけ。b のワークロードも private DNS で a の ENI に届く）
locals {
  endpoint_subnet_ids = var.endpoints_multi_az ? [aws_subnet.a.id, aws_subnet.b.id] : [aws_subnet.a.id]
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(var.interface_endpoints)

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = local.endpoint_subnet_ids
  security_group_ids  = [aws_security_group.endpoints.id]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OwnAccountOnly"
      Effect    = "Allow"
      Principal = "*"
      Action    = "*"
      Resource  = "*"
      Condition = {
        StringEquals = { "aws:PrincipalAccount" = local.account_id }
      }
    }]
  })

  tags = { Name = "${local.name_prefix}-${replace(each.key, ".", "-")}" }
}
