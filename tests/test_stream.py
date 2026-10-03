"""取り込みの経路（lab の機器 → Telegraf（ECS）→ MSK → Spark）の模擬テスト。lab の機器の設定・Telegraf の設定・terraform/pipeline/stream と、
Spark（spark/snmp_sinks.py）が異常の検知をしなくなったこと（2026-10-02。検知は Grafana のアラートルールと Splunk の保存済みサーチ → tests/test_alerts.py）を確かめる。
実行は python3 tests/test_stream.py（pyspark も boto3 も要らない。snmp_sinks.py は pyspark を関数の中で import する）。"""
import importlib.util, ipaddress, json, os, re, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "spark", "snmp_sinks.py")

passed = 0
def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)

# ---- terraform/pipeline/stream: 検知の資源（detector Lambda・DynamoDB の異常テーブル）は持たない
TF_DIR = os.path.join(ROOT, "terraform", "pipeline", "stream")
tf = ""
for name in sorted(os.listdir(TF_DIR)):
    if name.endswith(".tf"):
        with open(os.path.join(TF_DIR, name), encoding="utf-8") as f:
            tf += f.read() + "\n"
check("stream/detector.py は無い（検知は Grafana と Splunk）", not os.path.exists(os.path.join(ROOT, "stream", "detector.py")))
check("terraform/pipeline/stream に detector の Lambda が無い", '"detector"' not in tf and "stream/detector.py" not in tf and "archive_file" not in tf)
check("terraform/pipeline/stream に lambda のエンドポイントが無い", '.lambda"' not in tf)
check("terraform/pipeline/stream に MSK Connect の S3 sink が無い（Spark が S3 Tables に入れるので 2026-09-26 に削除。sts のエンドポイントも一緒に）",
      not os.path.exists(os.path.join(TF_DIR, "sink.tf")) and "mskconnect" not in tf and "create_s3_sink" not in tf
      and "kafkaconnect" not in tf and "create_sts_endpoint" not in tf and "aws_vpc_endpoint" not in tf)
check("terraform/pipeline/stream に DynamoDB が無い（2026-09-24）",
      "aws_dynamodb" not in tf and "anomaly_table" not in tf and "dynamodb:" not in tf and ".dynamodb" not in tf
      and not os.path.exists(os.path.join(TF_DIR, "anomalies.tf")))
check("detector_logs の output は無い", "detector_logs" not in tf)
check("MSK は Kafka 4 以上の KRaft（kafka_version の既定が N.N.x.kraft で、検査が .kraft を強いる）",
      re.search(r'variable "kafka_version" \{[^}]*default\s*=\s*"[4-9]\.\d+\.x\.kraft"', tf) is not None and "x\\\\.kraft$" in tf)
check("ブローカーは Kafka 4 が受け付ける m5 / m7g（t3.small は Unsupported InstanceType。2026-09-18）",
      re.search(r'variable "broker_instance_type" \{[^}]*default\s*=\s*"kafka\.m5\.large"', tf) is not None
      and '"kafka.t3.small"' not in tf)

# ---- spark/snmp_sinks.py を pyspark 無しで読む
spec = importlib.util.spec_from_file_location("snmp_sinks", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with open(SRC, encoding="utf-8") as f:
    src = f.read()
# 2026-10-02: Spark の検知（detect のクエリ・Neptune の anomaly の頂点・S3 Tables の anomaly_events・EventBridge への put_events）をやめた。
# 検知は Grafana のアラートルール（ポーリング）と Splunk の保存済みサーチ（trap と gNMI）が行い、SNS のトピックへ publish する
_code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#")).split('"""', 2)[2]   # 先頭の docstring とコメント行を除いたコード
check("Spark のジョブに検知の引数（--neptune-endpoint / --anomaly-events-table / --device-map / --event-bus / --event-source）は無い",
      not any(f'"--{a}"' in src for a in ("neptune-endpoint", "anomaly-events-table", "anomaly-table", "device-map", "event-bus", "event-source")))
check("Spark のジョブは Neptune にも EventBridge にも触らず、boto3 を読むのは SSM の HEC token だけ",
      not any(w in _code for w in ("put_events", "gremlin", "neptune", "anomaly", '"detect"', 'client("events")', 'client("dynamodb")'))
      and re.findall(r'boto3\.client\("(\w+)"', _code) == ["ssm"])
check("検知の関数と定数（events / device / parse_device_map / NeptuneAnomalies / make_detect_sender / EVENT_SOURCE / TRAP_TTL）はもう無い",
      not any(hasattr(mod, n) for n in ("events", "device", "parse_device_map", "anomaly_key", "anomaly_detail", "NeptuneAnomalies", "make_detect_sender",
                                        "make_history_writer", "gremlin_literal", "graphson", "EVENT_SOURCE", "EVENT_DETAIL_TYPE", "TRAP_TTL", "ANOMALY_EVENT_COLUMNS")))
BASE = ["--bootstrap", "b", "--checkpoint", "c", "--sinks", "iceberg", "--iceberg-table", "cat.ns.t"]
check("格納先は iceberg / opensearch / prometheus / splunk の 4 つで、マイクロバッチは 60 秒（アラートが届くまでの遅れの一部）",
      mod.SINKS == ("iceberg", "opensearch", "prometheus", "splunk") and mod.TRIGGER == "60 seconds" and mod.parse_args(BASE).sinks == ["iceberg"])
check("空のマイクロバッチでは sender を呼ばない（検知の見回りのための例外は無くなった）",
      re.search(r"\n\s*if records:\n\s*sender\(records\)", src) is not None and 'name == "detect"' not in src)


# ---- ログの経路: SR Linux の system logging remote-server（udp）→ lab の EC2（203.0.113.1:5140 を Telegraf の NLB へ DNAT）→ Telegraf（ECS）の inputs.syslog
# → Kafka の logs → Spark（2026-09-26。FRR + rsyslog をやめた）
def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()
srl_dir = os.path.join(ROOT, "lab", "srlinux")
srl_nodes = sorted(n[:-4] for n in os.listdir(srl_dir) if n.endswith(".cli"))
srl_cfg = {n: _read("lab", "srlinux", n + ".cli") for n in srl_nodes}
clab = _read("lab", "splab.clab.yml.in")
labsh = _read("lab", "lab.sh")
tele = _read("telegraf", "telegraf.conf.in")
tgsh = _read("telegraf", "telegraf.sh")
lab_locals = _read("terraform", "pipeline", "lab", "locals.tf")
stream_tg = _read("terraform", "pipeline", "stream", "telegraf.tf")
core_sg = _read("terraform", "base", "core", "security_groups.tf")
check("SR Linux の 6 台の設定は set / の行だけ（containerlab が候補に流し込んで commit する。enter candidate / commit を書くと二重になる）",
      len(srl_nodes) == 6 and all(all(re.match(r"^(set / |#|\s*$)", l) for l in c.splitlines()) for c in srl_cfg.values()))
check("containerlab は 6 台とも nokia_srlinux で srlinux/<機器名>.cli を startup-config にする",
      len(re.findall(r"^\s*kind: nokia_srlinux$", clab, re.M)) == 6
      and all(f"startup-config: srlinux/{n}.cli" in clab for n in srl_nodes) and "snmpd" not in clab and "binds:" not in clab)
mgmt_gw = re.search(r"^MGMT_GW=(\S+)$", labsh, re.M).group(1)
log_port = re.search(r"^LOG_PORT=(\d+)$", labsh, re.M).group(1)
check("6 台とも syslog を lab.sh の MGMT_GW:LOG_PORT/udp に送る（forward が Telegraf へ DNAT する）",
      all(f"set / system logging remote-server {mgmt_gw} transport udp" in c and f"set / system logging remote-server {mgmt_gw} remote-port {log_port}" in c
          and "set / system logging network-instance mgmt" in c for c in srl_cfg.values()))
check("6 台とも syslog のファシリティは local7（本番の Cisco の既定に合わせる。SR Linux の既定は local6）",
      all("set / system logging subsystem-facility local7" in c for c in srl_cfg.values()))
check("trap の宛先は 6 台とも lab.sh の MGMT_GW:162（Spine-Leaf の全部が監視対象）",
      all(f"destination telegraf address {mgmt_gw}" in srl_cfg[n] and "trap-group telegraf admin-state enable" in srl_cfg[n] for n in srl_nodes))
check("6 台とも IS-IS（instance main）と iBGP EVPN（AS 65100）を持ち、Spine だけ route-reflector",
      all("protocols isis instance main" in c and "protocols bgp autonomous-system 65100" in c and "afi-safi evpn admin-state enable" in c for c in srl_cfg.values())
      and all(("route-reflector client true" in srl_cfg[n]) == ("-spine-" in n) for n in srl_nodes))
check("containerlab の VM 2 台は linux で、leaf の組へ 2 本（bond）", len(re.findall(r"^\s*kind: linux$", clab, re.M)) == 2 and "bond0" in clab)
check("lab.sh forward は syslog の LOG_PORT も trap の 162 と同じ仕組みで DNAT する（rsyslog は無い）",
      re.search(r'-p udp --dport "\$LOG_PORT" "\$\{c\[@\]\}" -j DNAT --to-destination "\$t:\$LOG_PORT"', labsh) is not None
      and "rsyslog" not in labsh and "LOG_DIR" not in labsh and re.search(r"^\s*logs\)", labsh, re.M) is not None)
# ログのポートは 5 か所で同じ（lab.sh / telegraf.sh / telegraf.conf.in / stream の NLB / 土台の SG の通信の表）。trap は NLB の 162 → タスクの 1162（非 root）
check("syslog のポートが lab.sh・telegraf.sh・telegraf.conf.in・stream の NLB・土台の通信の表で同じで、trap は NLB の 162 をタスクの 1162 で受ける",
      re.search(rf"^LOG_PORT={log_port}$", tgsh, re.M) is not None and re.search(rf'^\s*server = "udp://:{log_port}"$', tele, re.M) is not None
      and re.search(r'^\s*service_address = "udp://:1162"$', tele, re.M) is not None and re.search(r"^TRAP_PORT=1162$", tgsh, re.M) is not None
      and all(re.search(rf'\{{ from = "{a}", to = "{b}", protocol = "udp", port = {pt},', core_sg) is not None
              for a, b, pt in (("lab_mgmt", "telegraf_nlb", log_port), ("lab", "telegraf_nlb", log_port), ("telegraf_nlb", "telegraf", log_port),
                               ("lab_mgmt", "telegraf_nlb", 162), ("lab", "telegraf_nlb", 162), ("telegraf_nlb", "telegraf", 1162)))
      and "log_port" not in lab_locals
      and re.search(rf"syslog = \{{ listener = {log_port}, container = {log_port} \}}", stream_tg) is not None
      and re.search(r"trap\s+= \{ listener = 162, container = 1162 \}", stream_tg) is not None)
# 管理ネットワークは 4 か所で同じ（containerlab の mgmt / lab.sh / lab の locals の VPC ルート / 土台の SG の lab_mgmt）
mgmt = re.search(r"^MGMT=(\S+)$", labsh, re.M).group(1)
check("管理ネットワークが containerlab・lab.sh・lab の locals・土台の SG の lab_mgmt_cidr で同じ",
      re.search(rf"^\s*ipv4-subnet: {re.escape(mgmt)}$", clab, re.M) is not None
      and re.search(rf'^\s*mgmt_cidr\s*=\s*"{re.escape(mgmt)}"$', lab_locals, re.M) is not None
      and re.search(rf'^\s*lab_mgmt_cidr\s*=\s*"{re.escape(mgmt)}"$', core_sg, re.M) is not None)
# ポーリング先は lab の定義から作る（lab/lab_topology.py --snmp-agents → up.sh が stream の snmp_agents → タスクの SNMP_AGENTS → telegraf.sh render が埋める）
_lt_spec = importlib.util.spec_from_file_location("lab_topology", os.path.join(ROOT, "lab", "lab_topology.py"))
lt = importlib.util.module_from_spec(_lt_spec); _lt_spec.loader.exec_module(lt)
_lab_devices, _, _ = lt.load(os.path.join(ROOT, "lab"))
_agents_line = lt.snmp_agents(_lab_devices)
_agents = re.findall(r"udp://([\d.]+):161", _agents_line)
check("Telegraf のポーリング先は lab の監視対象（enabled）の管理 IP で、全部管理ネットワークの中（VPC のルートで lab の EC2 へ行く）",
      len(_agents) == 6 and sorted(_agents) == sorted(d["mgmt_ip"] for d in _lab_devices if d["enabled"])
      and all(ipaddress.ip_address(a) in ipaddress.ip_network(mgmt) for a in _agents))
check("telegraf.conf.in の agents は __SNMP_AGENTS__ を telegraf.sh render がタスクの SNMP_AGENTS で埋める（形を確かめてから）",
      re.search(r"^\s*agents = \[__SNMP_AGENTS__\]$", tele, re.M) is not None and 's#__SNMP_AGENTS__#$agents#' in tgsh
      and 'agents="${SNMP_AGENTS:-}"' in tgsh and '{ name = "SNMP_AGENTS", value = var.snmp_agents }' in stream_tg
      and re.fullmatch(r'"udp://[0-9.]+:[0-9]+"(, *"udp://[0-9.]+:[0-9]+")*', _agents_line) is not None)
_gnmi_line = lt.gnmi_targets(_lab_devices)
check("gNMI の購読先は同じ 6 台の管理 IP:57400 で、telegraf.conf.in の __GNMI_TARGETS__ を telegraf.sh render がタスクの GNMI_TARGETS で埋める",
      re.findall(r"([\d.]+):57400", _gnmi_line) == _agents and re.search(r"^\s*addresses = \[__GNMI_TARGETS__\]$", tele, re.M) is not None
      and 's#__GNMI_TARGETS__#$gnmi#' in tgsh and 'gnmi="${GNMI_TARGETS:-}"' in tgsh and '{ name = "GNMI_TARGETS", value = var.gnmi_targets }' in stream_tg
      and re.fullmatch(r'"[0-9.]+:[0-9]+"(, *"[0-9.]+:[0-9]+")*', _gnmi_line) is not None)
gnmi_blk = tele.split("[[inputs.gnmi]]", 1)[1].split("# ----", 1)[0]
check("inputs.gnmi は TLS（自己署名）で bgp_neighbor / isis_interface（IS-IS の IF の oper-state。隣接そのものは消えるので取らない）を on_change、evpn_es / mac_table を 30 秒の sample で購読する",
      re.search(r'^\s*tls_enable = true$', gnmi_blk, re.M) is not None and re.search(r'^\s*enable_tls', gnmi_blk, re.M) is None and 'insecure_skip_verify = true' in gnmi_blk and 'encoding = "json_ietf"' in gnmi_blk
      and re.search(r'name = "bgp_neighbor"\s*\n\s*path = "/network-instance\[name=default\]/protocols/bgp/neighbor\[peer-address=\*\]/session-state"\s*\n\s*subscription_mode = "on_change"', gnmi_blk)
      and re.search(r'name = "isis_interface"\s*\n\s*path = "/network-instance\[name=default\]/protocols/isis/instance\[name=main\]/interface\[interface-name=\*\]/oper-state"\s*\n\s*subscription_mode = "on_change"', gnmi_blk)
      and 'name = "isis_adjacency"' not in gnmi_blk
      and gnmi_blk.count('subscription_mode = "sample"') == 2 and gnmi_blk.count('sample_interval = "30s"') == 2)
check("gNMI の 4 つは gnmi トピックへ（metrics には混ざらない）",
      re.search(r'topic = "gnmi"[\s\S]*?namepass = \["bgp_neighbor", "isis_interface", "evpn_es", "mac_table"\]', tele) is not None
      and re.search(r'topic = "metrics"[\s\S]*?namepass = \["system", "interface"\]', tele) is not None)
check("lab.sh forward は gNMI の GNMI_PORT/tcp も SNMP の 161/udp と同じく Telegraf から管理ネットワークへ通す",
      re.search(r'-p tcp --dport "\$GNMI_PORT" "\$\{c\[@\]\}" -j ACCEPT', labsh) is not None and re.search(r"^GNMI_PORT=57400$", labsh, re.M) is not None)
_up = _read("ops", "up.sh")
check("syslog の形式は stream の syslog_standard（既定 RFC3164 = 本番の Cisco）→ タスクの SYSLOG_STANDARD → telegraf.conf.in の __SYSLOG_STANDARD__。up.sh も deploy.env の SYSLOG_STANDARD（既定 RFC3164。lab の SR Linux は RFC5424）を渡す",
      re.search(r'^\s*syslog_standard = "__SYSLOG_STANDARD__"$', tele, re.M) is not None and 's#__SYSLOG_STANDARD__#$SYSLOG_STANDARD#' in tgsh
      and '{ name = "SYSLOG_STANDARD", value = var.syslog_standard }' in stream_tg
      and re.search(r'variable "syslog_standard" \{[^}]*default\s*=\s*"RFC3164"', _read("terraform", "pipeline", "stream", "variables.tf")) is not None
      and '-var "syslog_standard=$SYSLOG_STANDARD"' in _up and 'SYSLOG_STANDARD="${SYSLOG_STANDARD:-RFC3164}"' in _up and '[ "$SYSLOG_STANDARD" != "$LAB_SYSLOG_STANDARD" ]' in _up
      and "case \"$SYSLOG_STANDARD\" in RFC3164 | RFC5424) ;;" in _up
      and re.search(r"\bSYSLOG_STANDARD\b", _read("ops", "deploy-env.sh").split("DEPLOY_ENV_KEYS=", 1)[1].split('"')[1]) is not None
      and re.search(r"^#SYSLOG_STANDARD=RFC5424$", _read("deploy.env.example"), re.M) is not None and re.search(r"^LAB_SYSLOG_STANDARD=RFC5424\b", _read("ops", "lab-common.sh"), re.M) is not None)
check("SNMP のポーリングは既定で止める（trap だけ。2026-10-04 ユーザー決定）: stream の snmp_poll（bool、既定 false）→ タスクの SNMP_POLL（1 / 0）→ telegraf.sh が「>>> snmp_poll」の区間を残すか消す。"
      "up.sh は deploy.env の SNMP_POLL（既定 0）を渡す",
      re.search(r'variable "snmp_poll" \{[^}]*type\s*=\s*bool[^}]*default\s*=\s*false', _read("terraform", "pipeline", "stream", "variables.tf")) is not None
      and '{ name = "SNMP_POLL", value = var.snmp_poll ? "1" : "0" }' in stream_tg
      and re.search(r"^SNMP_POLL=\$\{SNMP_POLL:-0\}$", tgsh, re.M) is not None and '/^# >>> snmp_poll/,/^# <<< snmp_poll/d' in tgsh
      and re.search(r"^# >>> snmp_poll[\s\S]*?^\[\[inputs\.snmp\]\][\s\S]*?^# <<< snmp_poll", tele, re.M) is not None
      and "flag_value SNMP_POLL" in _up and '-var "snmp_poll=$SNMP_POLL_TF"' in _up
      and re.search(r"\bSNMP_POLL\b", _read("ops", "deploy-env.sh").split("DEPLOY_ENV_KEYS=", 1)[1].split('"')[1]) is not None
      and re.search(r"^#SNMP_POLL=1$", _read("deploy.env.example"), re.M) is not None)
check("Telegraf に入るコマンドの既定は tg gnmi（tg test はポーリングを止めていると何も取らない）",
      "--command 'tg gnmi'" in _read("terraform", "pipeline", "stream", "outputs.tf"))
check("up.sh は lab の定義からポーリング先と gNMI の購読先を作り、stream の snmp_agents / gnmi_targets に渡す（S3 には置かない）",
      'lab/lab_topology.py lab --snmp-agents' in _up and 'lab/lab_topology.py lab --gnmi-targets' in _up
      and '-var "snmp_agents=$SNMP_AGENTS" -var "gnmi_targets=$GNMI_TARGETS"' in _up and "/telegraf/" not in _up)
check("lab.sh up は毎回 forward を呼び、forward / forward-status がある",
      '"$SELF" forward' in labsh and re.search(r"^\s*forward\)", labsh, re.M) is not None and re.search(r"^\s*forward-status\)", labsh, re.M) is not None)
check("forward の iptables の規則は全部目印付き（unforward で消せる）",
      all("${c[@]}" in l for l in labsh.splitlines() if re.match(r"\s*iptables .*-I ", l)))
check("Telegraf はポーリングの IF の鍵を ifName（タグ）にする（SR Linux の ifDescr は description 付き）",
      re.search(r'name = "ifName"\s*\n\s*oid = "\.1\.3\.6\.1\.2\.1\.31\.1\.1\.1\.1"\s*\n\s*is_tag = true', tele) is not None)
check("Telegraf は機器の syslog を inputs.syslog（udp）で受け、device_log として logs トピックに出す",
      'name_override = "device_log"' in tele and "[[inputs.tail]]" not in tele and "[[inputs.socket_listener]]" not in tele
      and re.search(r'topic = "logs"[\s\S]*?namepass = \["device_log"\]|namepass = \["device_log"\][\s\S]*?topic = "logs"', tele) is not None)
check("metrics / traps の出力に device_log が混ざらない（namepass / namedrop）",
      all(re.search(r"name(pass|drop)", blk) for blk in tele.split("[[outputs.kafka]]")[1:]))
check("syslog の hostname を sysName のタグに付け替える（metrics / traps と同じ機器名のタグ）",
      re.search(r'\[\[processors\.rename\]\]\s*\n\s*namepass = \["device_log"\]\s*\n\s*\[\[processors\.rename\.replace\]\]\s*\n\s*tag = "hostname"\s*\n\s*dest = "sysName"', tele) is not None)
check("Spark の既定は gnmi トピックも読む（iceberg / prometheus は metrics,gnmi、opensearch は traps,logs）", mod.METRIC_TOPICS == "metrics,gnmi"
      and mod.sink_topics("iceberg", mod.METRIC_TOPICS, mod.LOG_TOPICS) == "metrics,gnmi,traps,logs" and mod.sink_topics("prometheus", mod.METRIC_TOPICS, mod.LOG_TOPICS) == "metrics,gnmi")
_access = _read("terraform", "pipeline", "stream", "access.tf")
_lab_tg = _read("terraform", "pipeline", "lab", "telegraf.tf")
check("Telegraf は stream の ECS で、MSK への書き込みはタスクロール（lab の state のロールに頼らない。2026-09-28）",
      'resource "aws_iam_role" "telegraf_task"' in stream_tg and "kafka-cluster:WriteData" in stream_tg
      and "stream_produce" not in _access and "telegraf_role_name" not in _access
      and 'resource "aws_iam_role"' not in _lab_tg and 'resource "aws_instance"' not in _lab_tg)
check("lab と stream は SG も SG のルールも作らない（ポーリング・trap・syslog のルールは土台の通信の表。2026-09-29）",
      all('resource "aws_security_group"' not in t and "aws_vpc_security_group_" not in t
          for t in (_lab_tg, lab_locals, stream_tg, _access, _read("terraform", "pipeline", "stream", "msk.tf"), _read("terraform", "pipeline", "lab", "instance.tf")))
      and 'security_groups = [local.telegraf_nlb_sg_id]' in stream_tg and 'security_groups  = [local.telegraf_sg_id]' in stream_tg)
_down = _read("ops", "down.sh")
check("down.sh は stream の必須変数（snmp_agents / gnmi_targets）に形だけ合う値を渡して destroy する（telegraf.sh の形の検査と同じ）",
      re.search(r"destroy_root pipeline/stream -var 'snmp_agents=\"udp://[0-9.]+:161\"' -var 'gnmi_targets=\"[0-9.]+:57400\"'", _down) is not None)
check("Spark の既定は logs も読む", mod.LOG_TOPICS == "traps,logs"
      and mod.sink_topics("opensearch", mod.METRIC_TOPICS, mod.LOG_TOPICS) == "traps,logs")

print(f"通過 {passed} / 失敗 0")
