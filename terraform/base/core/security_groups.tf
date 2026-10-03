# ---------------------------------------------------------------- security groups
# ワークロードごとに SG を 1 つ（aws_security_group.workload）と、VPC エンドポイント用の endpoints。ルールは下の通信の表（local.sg_flows）から作り、
# 表に無い通信は受信も送信も通らない（aws_security_group は作るときに既定の全許可の送信ルールを消す）。
# SG もルールもここ（土台）にまとめる。相手の SG を参照するルールを 1 か所で書くためで、ほかのルートは outputs.security_group_ids から
# 自分の SG を読んで付けるだけ（ルールは作らない）。SG とルールに時間課金は無いので、機能を作らないときもそろえて作る。
# 相手の絞り込みは SG と IAM の両方で行う（MSK / Neptune は IAM 認証、S3 / ECR / Bedrock はロールのポリシーと perimeter.tf の VPC の外を拒む Deny）。
# DNS（VPC の +2）・IMDS・ECS のタスクメタデータ・Time Sync は SG の対象外なので表に無い。インターネットからの受信は SG 以前に経路が無い（vpc.tf）。
# EMR Serverless は 0.0.0.0/0 の受信ルールがある SG を拒むが、表に CIDR の受信は lab の管理ネットワークと MDT の送り元（var.mdt_source_cidrs。
# NLB の SG だけ。0.0.0.0/0 は変数の検査で拒む）しか無い。
# 開けていないもの: Temporal の gRPC 7233（ワーカーは同じタスクの localhost。terraform/workflow の ecs.tf）、Splunk の管理 API 8089（外から使わない）。
# 2026-09-26〜09-29 は全部で internal 1 つ（VPC の中は何でも受け、送信は自由）だった。その前（7c42b0f）はルートごとに SG とルールを持っていた。
# SG の description は変えると作り直しになる（付いている ENI があると消えない）ので、変えるときは down してから
locals {
  # SG を付けるワークロード。名前は <接頭辞>-<キーの _ を - に>
  security_groups = {
    web = "Chat web EC2 - SSM port forwarding to Grafana, Splunk and the Temporal UI starts here"
    lab = "Lab EC2 - containerlab, forwards the lab mgmt network"
    # telegraf は受ける側（dial-out。NLB の後ろ）、telegraf_poll は取りにいく側（dial-in。2026-10-04 に分けた）。
    # telegraf の description は分ける前のまま（変えると作り直しになる。上の注記）
    telegraf      = "Telegraf ECS task (terraform/pipeline/stream)"
    telegraf_poll = "Telegraf poller ECS task - gNMI and SNMP polling (terraform/pipeline/stream)"
    telegraf_nlb  = "Internal NLB in front of Telegraf - traps and syslog (terraform/pipeline/stream)"
    msk           = "MSK brokers (terraform/pipeline/stream)"
    spark         = "EMR Serverless workers (terraform/pipeline/analytics)"
    grafana       = "Grafana ECS task (terraform/pipeline/analytics)"
    splunk        = "Splunk ECS task (terraform/pipeline/analytics)"
    neptune       = "Neptune (terraform/pipeline/graph)"
    lambda        = "Lambda in the VPC - graph status, MCP tools, knowledge base index"
    workflow      = "Temporal dev server and worker ECS task (terraform/workflow)"
    runtime       = "AgentCore Runtime ENIs (terraform/agent)"
  }
  sg_keys = concat(keys(local.security_groups), ["endpoints"])
  sg_ids  = merge({ for k, sg in aws_security_group.workload : k => sg.id }, { endpoints = aws_security_group.endpoints.id })

  # lab の管理ネットワーク。terraform/pipeline/lab の local.mgmt_cidr と lab/ の機器の設定と同じ値（tests/test_analytics.py が見る）
  lab_mgmt_cidr = "203.0.113.0/24"

  # AWS の API（インターフェース型と OpenSearch Serverless の VPC エンドポイント）と S3（ゲートウェイエンドポイント。S3 Tables のデータ・ECR のレイヤー・
  # AL2023 の dnf もここ）へ出るワークロード。Fargate のタスクは ECR のイメージ・SSM のシークレット・ログもタスクの ENI で取りに行く
  aws_api_clients = ["web", "lab", "telegraf", "telegraf_poll", "spark", "grafana", "splunk", "lambda", "workflow", "runtime"]

  # 通信の表。1 行が 1 つの流れで、from が送り、to が受ける（応答は SG の接続追跡で通るので書かない）。from / to は上の SG のキーか endpoints、
  # または SG でない相手の s3（S3 のマネージドプレフィックスリスト）と lab_mgmt（local.lab_mgmt_cidr）。
  # 送り元が CIDR の行は from を "cidr:<CIDR>"（ルールの鍵を分けるため）にして cidr に CIDR を書く（受信だけ）。
  # 両端が SG なら from の送信ルールと to の受信ルールの 2 本、片方だけが SG ならその側の 1 本になる。
  # only = "egress" / "ingress" は片側だけを書く行。lab の EC2 が管理ネットワークとのあいだを転送する流れは、SG が見る IP が
  # lab の EC2 のものでなく機器の管理 IP なので、SG の参照が効く側（相手の ENI の IP が見える側）だけを SG で書き、反対側は lab_mgmt の CIDR で書く
  sg_flows = flatten([
    [for sg in local.aws_api_clients : [
      { from = sg, to = "endpoints", protocol = "tcp", port = 443, why = "AWS APIs through the interface endpoints" },
      { from = sg, to = "s3", protocol = "tcp", port = 443, why = "S3 through the gateway endpoint" },
    ]],
    [
      # Gremlin（Neptune は IAM 認証）。無いと ops/up.sh の seed_graph.py が接続の待ちで止まる（2026-09-17）
      { from = "web", to = "neptune", protocol = "tcp", port = 8182, why = "Gremlin - topology view and seed_graph.py" },
      { from = "runtime", to = "neptune", protocol = "tcp", port = 8182, why = "Gremlin - agent tools" },
      { from = "lambda", to = "neptune", protocol = "tcp", port = 8182, why = "Gremlin - graph status and MCP tools" },
      { from = "workflow", to = "neptune", protocol = "tcp", port = 8182, why = "Gremlin - workflow activities" },

      # SSM のポートフォワーディング（利用者の PC → ssmmessages → Web の EC2 の SSM Agent → タスク）
      { from = "web", to = "grafana", protocol = "tcp", port = 3000, why = "Grafana UI through SSM port forwarding" },
      { from = "web", to = "splunk", protocol = "tcp", port = 8000, why = "Splunk Web through SSM port forwarding" },
      { from = "web", to = "workflow", protocol = "tcp", port = 8233, why = "Temporal UI through SSM port forwarding" },

      # Kafka（IAM 認証の 9098）
      { from = "telegraf", to = "msk", protocol = "tcp", port = 9098, why = "Kafka IAM - Telegraf writes" },
      { from = "telegraf_poll", to = "msk", protocol = "tcp", port = 9098, why = "Kafka IAM - Telegraf poller writes" },
      { from = "spark", to = "msk", protocol = "tcp", port = 9098, why = "Kafka IAM - Spark reads" },
      { from = "msk", to = "msk", protocol = "tcp", port = 9092, to_port = 9098, why = "Brokers talk to each other" },

      # Spark
      { from = "spark", to = "spark", protocol = "tcp", port = 0, to_port = 65535, why = "Driver and executors of one job" },
      { from = "spark", to = "splunk", protocol = "tcp", port = 8088, why = "Splunk HTTP Event Collector - splunk sink" },

      # Telegraf の NLB → タスク（terraform/pipeline/stream の telegraf.tf）。NLB が送り元の IP を残しても、タスクの受信は NLB の SG の参照で通る。
      # NLB の送信ルールは転送とヘルスチェックの両方に効く
      { from = "telegraf_nlb", to = "telegraf", protocol = "udp", port = 1162, why = "SNMP traps - NLB 162 to the task 1162" },
      { from = "telegraf_nlb", to = "telegraf", protocol = "udp", port = 5140, why = "syslog - NLB 5140 to the task 5140" },
      { from = "telegraf_nlb", to = "telegraf", protocol = "tcp", port = 57000, why = "Cisco MDT dial-out - NLB 57000 to the task 57000" },
      { from = "telegraf_nlb", to = "telegraf", protocol = "tcp", port = 8080, why = "NLB health check - Telegraf outputs.health" },

      # trap と syslog: 機器 → lab の EC2（lab.sh forward の DNAT）→ NLB。送り元は機器の管理 IP のままなので、NLB は管理ネットワークの CIDR から受け、
      # lab の EC2 は NLB の SG へ送る
      { from = "lab_mgmt", to = "telegraf_nlb", protocol = "udp", port = 162, why = "SNMP traps from the switches - DNAT on the lab EC2" },
      { from = "lab_mgmt", to = "telegraf_nlb", protocol = "udp", port = 5140, why = "syslog from the switches - DNAT on the lab EC2" },
      { from = "lab", to = "telegraf_nlb", protocol = "udp", port = 162, only = "egress", why = "SNMP traps forwarded for the switches" },
      { from = "lab", to = "telegraf_nlb", protocol = "udp", port = 5140, only = "egress", why = "syslog forwarded for the switches" },

      # ポーリング: 取りにいくタスク（telegraf_poll）→ 機器の SNMP と gNMI（VPC のルートで lab の EC2 へ。terraform/pipeline/lab の telegraf.tf）。
      # タスクは管理ネットワークの CIDR へ送り、lab の EC2 はタスクの SG から受ける。受ける側のタスク（telegraf）は機器へ出ない
      { from = "telegraf_poll", to = "lab_mgmt", protocol = "udp", port = 161, why = "SNMP polling of the switches" },
      { from = "telegraf_poll", to = "lab_mgmt", protocol = "tcp", port = 57400, why = "gNMI subscription to the switches" },
      { from = "telegraf_poll", to = "lab", protocol = "udp", port = 161, only = "ingress", why = "SNMP polling forwarded to the switches" },
      { from = "telegraf_poll", to = "lab", protocol = "tcp", port = 57400, only = "ingress", why = "gNMI forwarded to the switches" },
    ],
    # MDT の dial-out: 本番の Cisco → NLB の 57000/tcp（docs/collection.md）。送り元の CIDR は変数（既定は空で、どこからも受けない）。
    # lab の SR Linux は MDT を送れないので、lab の管理ネットワークからは開けない
    [for c in var.mdt_source_cidrs :
      { from = "cidr:${c}", cidr = c, to = "telegraf_nlb", protocol = "tcp", port = 57000, why = "Cisco MDT dial-out from the devices (mdt_source_cidrs)" }
    ],
  ])

  sg_rules = { for f in local.sg_flows : "${f.from}-${f.to}-${f.protocol}-${f.port}" => {
    from     = f.from
    to       = f.to
    protocol = f.protocol
    port     = f.port
    to_port  = try(f.to_port, f.port)
    only     = try(f.only, "")
    cidr     = try(f.cidr, null)
    why      = f.why
  } }
}

data "aws_ec2_managed_prefix_list" "s3" {
  name = "com.amazonaws.${var.region}.s3"
}

resource "aws_security_group" "workload" {
  for_each = local.security_groups

  name        = "${local.name_prefix}-${replace(each.key, "_", "-")}"
  description = each.value
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-${replace(each.key, "_", "-")}" }
}

resource "aws_vpc_security_group_egress_rule" "flow" {
  for_each = { for k, r in local.sg_rules : k => r if contains(local.sg_keys, r.from) && r.only != "ingress" }

  security_group_id            = local.sg_ids[each.value.from]
  description                  = each.value.why
  ip_protocol                  = each.value.protocol
  from_port                    = each.value.port
  to_port                      = each.value.to_port
  referenced_security_group_id = contains(local.sg_keys, each.value.to) ? local.sg_ids[each.value.to] : null
  prefix_list_id               = each.value.to == "s3" ? data.aws_ec2_managed_prefix_list.s3.id : null
  cidr_ipv4                    = each.value.to == "lab_mgmt" ? local.lab_mgmt_cidr : null
}

resource "aws_vpc_security_group_ingress_rule" "flow" {
  for_each = { for k, r in local.sg_rules : k => r if contains(local.sg_keys, r.to) && r.only != "egress" }

  security_group_id            = local.sg_ids[each.value.to]
  description                  = each.value.why
  ip_protocol                  = each.value.protocol
  from_port                    = each.value.port
  to_port                      = each.value.to_port
  referenced_security_group_id = contains(local.sg_keys, each.value.from) ? local.sg_ids[each.value.from] : null
  cidr_ipv4                    = each.value.from == "lab_mgmt" ? local.lab_mgmt_cidr : each.value.cidr
}

# インターフェース型と OpenSearch Serverless の VPC エンドポイント（endpoints.tf）に付ける。受信は表の 443 だけ
resource "aws_security_group" "endpoints" {
  name        = "${local.name_prefix}-endpoints"
  description = "VPC endpoints - HTTPS from the workload SGs, no outbound"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name_prefix}-endpoints" }
}

resource "aws_vpc_security_group_egress_rule" "endpoints_none" {
  security_group_id = aws_security_group.endpoints.id
  description       = "No outbound"
  ip_protocol       = "-1"
  cidr_ipv4         = "127.0.0.1/32"
}
