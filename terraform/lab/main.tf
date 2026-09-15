# fukuda-nwc-poc - optional lab root module. One EC2 (Amazon Linux 2023 arm64) runs Docker + containerlab with the wvs2 topology
# (6 FRR routers with BGP, 4 snmpd sidecars, 4 hosts, all fictional addresses). Reached with SSM Session Manager.
# Images come from ECR (terraform/ecr), the containerlab rpm and configs from the S3 bucket of terraform/main. Stop the instance when not in use.
# Phase 2: if the Telegraf rpm is also in lab/, Telegraf polls the CE routers over SNMP, receives their traps and writes to MSK (terraform/stream).

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_ssm_parameter" "al2023" {
  name = var.ami_ssm_parameter
}

# VPC / subnet / endpoint SG / bucket は terraform/main の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../main/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id         = data.terraform_remote_state.main.outputs.vpc_id
  subnet_id      = data.terraform_remote_state.main.outputs.instance_subnet_id
  endpoint_sg_id = data.terraform_remote_state.main.outputs.endpoint_security_group_id
  bucket         = data.terraform_remote_state.main.outputs.kb_bucket_name
}

# 受信ルールは置かない。操作は SSM Session Manager（SSM Agent が内側から ssmmessages へつなぎに行く）。
# lab のアドレス（203.0.113.0/24 / 172.16.0.0/16 / 10.x.0.0/24）は EC2 の中の docker network と veth に閉じていて VPC には出ない
resource "aws_security_group" "lab" {
  name        = "${var.name_prefix}-lab"
  description = "Lab EC2 - no inbound, outbound HTTPS only (SSM, ECR, S3 gateway)"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name_prefix}-lab" }
}

resource "aws_vpc_security_group_egress_rule" "lab_https" {
  security_group_id = aws_security_group.lab.id
  description       = "HTTPS to VPC endpoints and S3 gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "lab_kafka" {
  security_group_id = aws_security_group.lab.id
  description       = "Phase 2 - Kafka with IAM auth to the MSK brokers of terraform/stream (private IPs in this VPC, no NAT)"
  ip_protocol       = "tcp"
  from_port         = 9098
  to_port           = 9098
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_lab" {
  security_group_id            = local.endpoint_sg_id
  description                  = "HTTPS from lab EC2 (ssm, ssmmessages, ecr.api, ecr.dkr)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.lab.id
}

resource "aws_iam_role" "lab" {
  name        = "${var.name_prefix}-lab"
  description = "fukuda-nwc-poc lab EC2 - SSM managed node, pull lab images from ECR, read lab/ from the asset bucket"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${var.name_prefix}-lab" }
}

resource "aws_iam_role_policy_attachment" "lab_ssm" {
  role       = aws_iam_role.lab.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "lab_assets" {
  name = "lab-assets"
  role = aws_iam_role.lab.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrPull"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = "arn:${local.partition}:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}-lab-*"
      },
      {
        Sid      = "EcrToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "S3Read"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:${local.partition}:s3:::${local.bucket}/lab/*"
      },
      {
        Sid      = "S3List"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:${local.partition}:s3:::${local.bucket}"
        Condition = {
          StringLike = { "s3:prefix" = "lab/*" }
        }
      },
    ]
  })
}

resource "aws_iam_instance_profile" "lab" {
  name = "${var.name_prefix}-lab"
  role = aws_iam_role.lab.name
}

# 起動のたびに流す（cloud-config の always）。S3 の lab/ を置き直して再起動すれば設定も更新される
resource "aws_instance" "lab" {
  ami                         = data.aws_ssm_parameter.al2023.insecure_value
  instance_type               = var.instance_type
  iam_instance_profile        = aws_iam_instance_profile.lab.name
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.lab.id]
  associate_public_ip_address = false

  user_data = templatefile("${path.module}/templates/lab_user_data.sh.tftpl", {
    name_prefix          = var.name_prefix
    region               = var.region
    account_id           = local.account_id
    bucket               = local.bucket
    containerlab_version = var.containerlab_version
    telegraf_version     = var.telegraf_version
    frr_image_tag        = var.frr_image_tag
    snmpd_image_tag      = var.snmpd_image_tag
    multitool_image_tag  = var.multitool_image_tag
    auto_start_lab       = var.auto_start_lab ? "true" : "false"
  })
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
    # コンテナからは IMDS に届かせない（hop limit 1 は docker bridge を越えない）
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.volume_size
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name    = "${var.name_prefix}-lab"
      Project = var.name_prefix
      owner   = var.owner
    }
  }

  tags = { Name = "${var.name_prefix}-lab" }

  depends_on = [
    aws_iam_role_policy_attachment.lab_ssm,
    aws_iam_role_policy.lab_assets,
    aws_vpc_security_group_egress_rule.lab_https,
    aws_vpc_security_group_ingress_rule.endpoints_from_lab,
  ]
}
