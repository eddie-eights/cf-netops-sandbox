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
