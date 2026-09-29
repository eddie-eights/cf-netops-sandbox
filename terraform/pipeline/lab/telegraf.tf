# ---------------------------------------------------------------- path to Telegraf (forward_to_telegraf)
# Telegraf は terraform/pipeline/stream の ECS のタスク（内部 NLB の後ろ。2026-09-28 まではここで作っていた EC2）。
# lab の管理ネットワーク（local.mgmt_cidr）は lab の EC2 の中の docker network で VPC からは見えないので、次の 3 つで届ける:
#   ポーリング  Telegraf → 機器の SNMP（203.0.113.11〜32:161/udp）と gNMI（57400/tcp。BGP / IS-IS / EVPN の状態）。下の aws_route でこの宛先を lab の EC2 へ向け、
#               lab の EC2 の上で lab.sh forward が Docker の DOCKER-USER に通す穴を開ける（送り元はタスクのサブネットの CIDR。タスクの IP は作り直すたびに変わる）
#   trap        機器 → 203.0.113.1:162/udp（lab の EC2）。lab.sh forward が Telegraf の NLB へ DNAT し、NLB がタスクの 1162 へ向ける。送り元（機器の管理 IP）は
#               Docker の MASQUERADE にかけず、NLB も残す（Spark とエージェントは送り元の IP で機器を引く）
#   syslog      機器 → 203.0.113.1:5140/udp（lab の EC2。lab/lab.sh の LOG_PORT）。trap と同じ仕組みで NLB の 5140 → タスクの 5140
# NLB のアドレスとタスクのサブネットの CIDR は terraform/pipeline/stream が SSM の /<接頭辞>/telegraf-address と telegraf-source-cidr に書く（lab.sh forward が読む）。
# stream を後から作っても lab の EC2 は作り直さない（ops/up.sh の 7-2b が lab forward を打ち直す）

# SG のルールは terraform/base/core の security_groups.tf の通信の表にある（ここでは作らない）:
#   ポーリング  タスクの SG → 管理ネットワークの CIDR（udp 161 / tcp 57400）、lab の EC2 はタスクの SG から受ける
#   trap / syslog  lab の EC2 → NLB の SG（udp 162 / 5140）、NLB は管理ネットワークの CIDR から受ける（送り元が機器の管理 IP のままなので）
# SNMP と gNMI の応答はタスクの送信の戻りなので SG の追跡で通る。2026-09-29 まではここで internal の SG に管理ネットワークからの受信を足していた

# 管理ネットワーク宛てを lab の EC2 へ。lab の EC2 は source_dest_check を切る（instance.tf）。
# 送り元が 203.0.113.x の応答と trap を VPC に出すため
resource "aws_route" "lab_mgmt" {
  for_each = var.forward_to_telegraf ? toset(local.route_table_ids) : toset([])

  route_table_id         = each.value
  destination_cidr_block = local.mgmt_cidr
  network_interface_id   = aws_instance.lab.primary_network_interface_id
}
