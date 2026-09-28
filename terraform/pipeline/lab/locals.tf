# netops-poc - optional lab root module. One EC2 (Amazon Linux 2023 arm64) runs Docker + containerlab with the splab topology
# (Spine-Leaf: 6 Nokia SR Linux switches with IS-IS + iBGP EVPN-VXLAN, 2 VMs dual-homed with LACP, all fictional addresses. lab/gen_lab.py). Reached with SSM Session Manager.
# Images come from ECR (terraform/base/ecr), the containerlab rpm and configs from the S3 bucket of terraform/base/core. Stop the instance when not in use.
# With forward_to_telegraf (telegraf.tf), the VPC routes the lab mgmt network to this EC2 so Telegraf (an ECS task of terraform/pipeline/stream)
# can poll the switches over SNMP and gNMI, and lab.sh forward sends their traps and syslog to the Telegraf NLB.

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_ssm_parameter" "al2023" {
  name = var.ami_ssm_parameter
}

# VPC / subnet / SG / bucket は terraform/base/core の state から読む
data "terraform_remote_state" "main" {
  backend = "local"

  config = {
    path = "${path.module}/../../base/core/terraform.tfstate"
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  vpc_id         = data.terraform_remote_state.main.outputs.vpc_id
  subnet_id      = data.terraform_remote_state.main.outputs.instance_subnet_id
  internal_sg_id = data.terraform_remote_state.main.outputs.internal_security_group_id
  bucket         = data.terraform_remote_state.main.outputs.kb_bucket_name
  # terraform/base/core の perimeter.tf の Deny（VPC エンドポイントを通らない AWS の API を拒む）。NETWORK_PERIMETER=0 か古い state なら空
  perimeter_policy_arn = try(data.terraform_remote_state.main.outputs.network_perimeter_policy_arn, "")
  # 1 本（terraform/base/core の private）。Telegraf のタスクから lab の管理ネットワークへの経路を足す
  route_table_ids = data.terraform_remote_state.main.outputs.route_table_ids

  # containerlab の管理ネットワーク（lab/splab.clab.yml.in の mgmt と lab/lab.sh の MGMT と同じ）。EC2 の中の docker network で、
  # forward_to_telegraf のときだけ VPC のルートで lab の EC2 に向ける（Telegraf のタスクから機器の SNMP と gNMI を引くため）
  mgmt_cidr = "203.0.113.0/24"
  # 機器の syslog（SR Linux の remote-server）を受ける UDP のポート。機器は lab の EC2（203.0.113.1）へ送り、lab.sh forward が trap の 162 と同じように
  # Telegraf の NLB へ DNAT する（lab/lab.sh の LOG_PORT と telegraf/telegraf.conf.in の inputs.syslog と同じ）
  log_port = 5140
}
