# ---------------------------------------------------------------- chat web EC2
data "aws_ssm_parameter" "al2023" {
  name = var.ami_ssm_parameter
}

resource "aws_iam_role" "web" {
  name        = "${local.name_prefix}-web"
  description = "${local.name_prefix} chat web EC2 - SSM managed node, reads web assets from S3 and the runtime ARN from SSM"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name_prefix}-web" }
}

resource "aws_iam_role_policy_attachment" "web_ssm" {
  role       = aws_iam_role.web.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# 画面のコード・静的データ・wheel は同じバケットの web/ に置く（ops/up.sh の手順 4）。docs/ は読ませない。
# Runtime の ARN は terraform/agent が /<接頭辞>/runtime-arn に書き、web/app.py が 60 秒ごとに読む（agent を後から入れ替えても再起動が要らない）。
# InvokeAgentRuntime の許可は terraform/agent がこのロールに足す
resource "aws_iam_role_policy" "web_assets" {
  name = "web-assets"
  role = aws_iam_role.web.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.kb.arn}/web/*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.kb.arn
        Condition = {
          StringLike = { "s3:prefix" = "web/*" }
        }
      },
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter/${local.name_prefix}/*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "web" {
  name = "${local.name_prefix}-web"
  role = aws_iam_role.web.name
}

# user_data を変えると Terraform はインスタンスを作り直す（user_data_replace_on_change）。
# 先頭の cloud-config で起動のたびにスクリプトを流すので、S3 の web/ を置き直して再起動すれば画面も更新される
resource "aws_instance" "web" {
  ami                         = data.aws_ssm_parameter.al2023.insecure_value
  instance_type               = var.instance_type
  iam_instance_profile        = aws_iam_instance_profile.web.name
  subnet_id                   = aws_subnet.a.id
  vpc_security_group_ids      = [aws_security_group.web.id]
  associate_public_ip_address = false

  user_data = templatefile("${path.module}/templates/web_user_data.sh.tftpl", {
    name_prefix = local.name_prefix
    region      = var.region
    bucket      = aws_s3_bucket.kb.bucket
  })
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 8
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name    = "${local.name_prefix}-web"
      Project = local.name_prefix
      owner   = var.owner
    }
  }

  tags = { Name = "${local.name_prefix}-web" }

  # 起動スクリプトが S3 gateway と ssm エンドポイントを使うので、先に作らせる
  depends_on = [
    aws_iam_role_policy_attachment.web_ssm,
    aws_iam_role_policy.web_assets,
    aws_vpc_endpoint.s3,
    aws_vpc_endpoint.ssm,
    aws_vpc_security_group_egress_rule.web_https,
    aws_vpc_security_group_ingress_rule.endpoints_from_web,
  ]
}
