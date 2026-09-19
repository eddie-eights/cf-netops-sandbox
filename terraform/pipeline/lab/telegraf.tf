# ---------------------------------------------------------------- Telegraf EC2 (create_telegraf)
# Telegraf を lab の EC2 から分けた小さな EC2。トポロジ（containerlab）と別に止める・作り直す・ログを見ることができる。
# lab の管理ネットワーク（local.mgmt_cidr）は lab の EC2 の中の docker network で VPC からは見えないので、次の 3 つで届ける:
#   ポーリング  Telegraf → CE の snmpd（203.0.113.11〜14:161/udp）。下の aws_route でこの宛先を lab の EC2 へ向け、
#               lab の EC2 の上で lab.sh forward が Docker の DOCKER-USER に通す穴を開ける
#   trap        snmpd → 203.0.113.1:162/udp（lab の EC2）。lab.sh forward が Telegraf へ DNAT する。送り元（機器の管理 IP）は
#               Docker の MASQUERADE にかけない（Spark とエージェントは送り元の IP で機器を引く）
#   FRR のログ  lab の EC2 の rsyslog（imfile）が 1 行ずつ「機器名 FRR の行」にして Telegraf の local.log_port/tcp へ送る
# Telegraf のアドレスは ENI を先に作って固定し、SSM の /<接頭辞>/telegraf-address に書く（lab.sh forward が読む）。
# Telegraf の EC2 を作り直しても（AMI の更新など）アドレスは変わらないので、lab の側は作り直さなくてよい。

resource "aws_security_group" "telegraf" {
  count = var.create_telegraf ? 1 : 0

  name        = "${local.name_prefix}-telegraf"
  description = "Telegraf EC2 - SNMP to the lab mgmt network, traps and FRR logs from the lab, Kafka IAM to MSK, HTTPS to endpoints"
  vpc_id      = local.vpc_id

  tags = { Name = "${local.name_prefix}-telegraf" }
}

resource "aws_vpc_security_group_egress_rule" "telegraf_https" {
  count = var.create_telegraf ? 1 : 0

  security_group_id = aws_security_group.telegraf[0].id
  description       = "HTTPS to VPC endpoints (ssm, ssmmessages) and S3 gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "telegraf_kafka" {
  count = var.create_telegraf ? 1 : 0

  security_group_id = aws_security_group.telegraf[0].id
  description       = "Kafka with IAM auth to the MSK brokers of terraform/pipeline/stream (private IPs in this VPC, no NAT)"
  ip_protocol       = "tcp"
  from_port         = 9098
  to_port           = 9098
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "telegraf_snmp" {
  count = var.create_telegraf ? 1 : 0

  security_group_id = aws_security_group.telegraf[0].id
  description       = "SNMP polling of the CE routers on the lab mgmt network (routed to the lab EC2)"
  ip_protocol       = "udp"
  from_port         = 161
  to_port           = 161
  cidr_ipv4         = local.mgmt_cidr
}

# trap は lab の EC2 が DNAT するだけで送り元は機器の管理 IP のまま。SG の参照（lab の ENI のアドレス）には当たらないので CIDR で許す
resource "aws_vpc_security_group_ingress_rule" "telegraf_trap" {
  count = var.create_telegraf ? 1 : 0

  security_group_id = aws_security_group.telegraf[0].id
  description       = "SNMP traps from the CE routers (DNAT on the lab EC2, source is the router mgmt IP)"
  ip_protocol       = "udp"
  from_port         = 162
  to_port           = 162
  cidr_ipv4         = local.mgmt_cidr
}

resource "aws_vpc_security_group_ingress_rule" "telegraf_logs" {
  count = var.create_telegraf ? 1 : 0

  security_group_id            = aws_security_group.telegraf[0].id
  description                  = "FRR log lines from rsyslog on the lab EC2"
  ip_protocol                  = "tcp"
  from_port                    = local.log_port
  to_port                      = local.log_port
  referenced_security_group_id = aws_security_group.lab.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_telegraf" {
  count = var.create_telegraf ? 1 : 0

  security_group_id            = local.endpoint_sg_id
  description                  = "HTTPS from Telegraf EC2 (ssm, ssmmessages of terraform/base/core)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.telegraf[0].id
}

# ---- lab の SG に足す穴（Telegraf があるときだけ）
resource "aws_vpc_security_group_ingress_rule" "lab_snmp_from_telegraf" {
  count = var.create_telegraf ? 1 : 0

  security_group_id            = aws_security_group.lab.id
  description                  = "SNMP polling from Telegraf EC2, forwarded to the CE routers on the mgmt network"
  ip_protocol                  = "udp"
  from_port                    = 161
  to_port                      = 161
  referenced_security_group_id = aws_security_group.telegraf[0].id
}

resource "aws_vpc_security_group_egress_rule" "lab_trap_to_telegraf" {
  count = var.create_telegraf ? 1 : 0

  security_group_id            = aws_security_group.lab.id
  description                  = "SNMP traps of the CE routers, DNATed to Telegraf EC2"
  ip_protocol                  = "udp"
  from_port                    = 162
  to_port                      = 162
  referenced_security_group_id = aws_security_group.telegraf[0].id
}

resource "aws_vpc_security_group_egress_rule" "lab_logs_to_telegraf" {
  count = var.create_telegraf ? 1 : 0

  security_group_id            = aws_security_group.lab.id
  description                  = "FRR log lines (rsyslog) to Telegraf EC2"
  ip_protocol                  = "tcp"
  from_port                    = local.log_port
  to_port                      = local.log_port
  referenced_security_group_id = aws_security_group.telegraf[0].id
}

# 管理ネットワーク宛てを lab の EC2 へ。lab の EC2 は source_dest_check を切る（instance.tf）。
# 送り元が 203.0.113.x の応答と trap を VPC に出すため
resource "aws_route" "lab_mgmt" {
  for_each = var.create_telegraf ? toset(local.route_table_ids) : toset([])

  route_table_id         = each.value
  destination_cidr_block = local.mgmt_cidr
  network_interface_id   = aws_instance.lab.primary_network_interface_id
}

# ---------------------------------------------------------------- IAM
resource "aws_iam_role" "telegraf" {
  count = var.create_telegraf ? 1 : 0

  name        = "${local.name_prefix}-telegraf"
  description = "${local.name_prefix} Telegraf EC2 - SSM managed node, read telegraf/ from the asset bucket. terraform/pipeline/stream adds the MSK write permissions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name_prefix}-telegraf" }
}

resource "aws_iam_role_policy_attachment" "telegraf_ssm" {
  count = var.create_telegraf ? 1 : 0

  role       = aws_iam_role.telegraf[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "telegraf_assets" {
  count = var.create_telegraf ? 1 : 0

  name = "telegraf-assets"
  role = aws_iam_role.telegraf[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "S3Read"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:${local.partition}:s3:::${local.bucket}/telegraf/*"
      },
      {
        Sid      = "S3List"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:${local.partition}:s3:::${local.bucket}"
        Condition = {
          StringLike = { "s3:prefix" = "telegraf/*" }
        }
      },
    ]
  })
}

resource "aws_iam_instance_profile" "telegraf" {
  count = var.create_telegraf ? 1 : 0

  name = "${local.name_prefix}-telegraf"
  role = aws_iam_role.telegraf[0].name
}

# ---------------------------------------------------------------- instance
resource "aws_network_interface" "telegraf" {
  count = var.create_telegraf ? 1 : 0

  subnet_id       = local.subnet_id
  security_groups = [aws_security_group.telegraf[0].id]
  description     = "${local.name_prefix} Telegraf - fixed address for the trap DNAT and rsyslog on the lab EC2"

  tags = { Name = "${local.name_prefix}-telegraf" }
}

resource "aws_ssm_parameter" "telegraf_address" {
  count = var.create_telegraf ? 1 : 0

  name        = "/${local.name_prefix}/telegraf-address"
  type        = "String"
  value       = aws_network_interface.telegraf[0].private_ip
  description = "Private IP of the Telegraf EC2. Read by lab.sh forward on the lab EC2 (trap DNAT and the rsyslog target)."
}

# 起動のたびに流す（cloud-config の always）。S3 の telegraf/ を置き直して再起動すれば設定も更新される
resource "aws_instance" "telegraf" {
  count = var.create_telegraf ? 1 : 0

  ami                  = data.aws_ssm_parameter.al2023.insecure_value
  instance_type        = var.telegraf_instance_type
  iam_instance_profile = aws_iam_instance_profile.telegraf[0].name

  primary_network_interface {
    network_interface_id = aws_network_interface.telegraf[0].id
  }

  user_data = templatefile("${path.module}/templates/telegraf_user_data.sh.tftpl", {
    name_prefix      = local.name_prefix
    region           = var.region
    bucket           = local.bucket
    telegraf_version = var.telegraf_version
  })
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name    = "${local.name_prefix}-telegraf"
      Project = local.name_prefix
      owner   = var.owner
    }
  }

  tags = { Name = "${local.name_prefix}-telegraf" }

  depends_on = [
    aws_iam_role_policy_attachment.telegraf_ssm,
    aws_iam_role_policy.telegraf_assets,
    aws_vpc_security_group_egress_rule.telegraf_https,
    aws_vpc_security_group_ingress_rule.endpoints_from_telegraf,
  ]
}
