# ---------------------------------------------------------------- network perimeter (2026-09-28)
# AWS の API は全部 VPC エンドポイント（endpoints.tf）を通すので、正しいリクエストには aws:SourceVpc = この VPC が付く。
# それが付かないリクエスト（NAT Gateway から出た、盗んだ認証情報を VPC の外で使った）を 2 か所で拒む:
#   1. IAM 側   ワークロードのロールに付ける Deny（下の network_perimeter）。付け先は web / runtime（ここ）、lab / Telegraf（pipeline/lab）、
#               Spark（pipeline/analytics）、ワーカー（workflow）、ツールの Lambda（workflow）
#   2. 資源側   バケット（下）、S3 Tables（pipeline/analytics）、SQS（workflow）のリソースポリシーの Deny
# 例外は 3 つ:
#   - デプロイする人（ops/up.sh を打つ PC の認証情報。terraform と s3 cp が VPC の外から来る。PoC では仕方ないとユーザーが決めた、2026-09-28）
#   - AWS のサービス自身（aws:PrincipalIsAWSService: EventBridge → SQS）とサービスが呼び手の代わりに出すリクエスト（aws:ViaAWSService）
#   - サービスがロールを引き受けて自分の側から来るもの（Bedrock の KB が docs/ を読む <prefix>-kb）
# IAM 側で拒む API は、VPC エンドポイントを通るものだけにする。logs / ecr / ec2 / kms / sts / xray はサービスがロールの認証情報で
# 自分の側から呼ぶ（Lambda のログ、Runtime のイメージ取得）ので入れない。neptune-db / kafka-cluster は VPC の中にしか無く、aoss はネットワークポリシーで閉じている。
# S3 Tables の Iceberg REST（/iceberg）は s3tables が呼び手の代わりに中で出す呼び出しに元の VPC を引き継がないので、
# aws:CalledViaLast = s3tables.amazonaws.com も外す（AWS の S3 Tables と VPC エンドポイントの文書の例と同じ）。
# AMP のワークスペースのリソースポリシーは Prometheus 互換の API の共有用で Deny と aws:SourceVpc が効くか確かめられないので、IAM 側だけで止める。
# 止めるときは NETWORK_PERIMETER=0 ops/up.sh（Deny を全部外す。インターフェース型エンドポイントは残る）
data "aws_iam_session_context" "deployer" {
  arn = data.aws_caller_identity.current.arn
}

locals {
  # 資源側の Deny から外すプリンシパル。issuer_arn は引き受けたロールの ARN（IAM ユーザーならその ARN）
  perimeter_exempt_principals = [
    data.aws_iam_session_context.deployer.issuer_arn,
    "arn:${local.partition}:iam::${local.account_id}:role/${local.name_prefix}-kb",
  ]

  # IAM 側で拒む API。どれも VPC エンドポイント（s3 は gateway）を通る
  perimeter_denied_actions = [
    "s3:*",
    "s3tables:*",
    "sqs:*",
    "ssm:*",
    "ssmmessages:*",
    "bedrock:*",
    "events:*",
    "aps:*",
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeGateway",
  ]
}

# ポリシーはいつも作り、network_perimeter = false のときは中身を何も拒まないものにする。消すと、ほかのルート（lab / analytics / workflow）の
# ロールに付いたままのうちは DeleteConflict で base/core の apply が落ちるため。ほかのルートは output が空になると次の apply で外す
resource "aws_iam_policy" "network_perimeter" {
  name        = "${local.name_prefix}-network-perimeter"
  description = "Deny AWS API calls that do not come through a VPC endpoint of ${local.name_prefix} (no-op while network_perimeter is false)"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [var.network_perimeter ? {
      Sid      = "DenyOutsideVpc"
      Effect   = "Deny"
      Action   = local.perimeter_denied_actions
      Resource = "*"
      Condition = {
        StringNotEqualsIfExists = { "aws:SourceVpc" = aws_vpc.this.id, "aws:CalledViaLast" = "s3tables.amazonaws.com" }
        BoolIfExists            = { "aws:ViaAWSService" = "false" }
      }
      } : {
      # 無効のとき。GetCallerIdentity は許可が無くても通るので、付けても何も変わらない
      Sid       = "Disabled"
      Effect    = "Allow"
      Action    = ["sts:GetCallerIdentity"]
      Resource  = "*"
      Condition = {}
    }]
  })
}

resource "aws_iam_role_policy_attachment" "web_perimeter" {
  count = var.network_perimeter ? 1 : 0

  role       = aws_iam_role.web.name
  policy_arn = aws_iam_policy.network_perimeter.arn
}

resource "aws_iam_role_policy_attachment" "runtime_perimeter" {
  count = var.network_perimeter ? 1 : 0

  role       = aws_iam_role.runtime.name
  policy_arn = aws_iam_policy.network_perimeter.arn
}
