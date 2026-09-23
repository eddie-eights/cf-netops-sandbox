"""Neptune へのトポロジ同期の模擬テスト（AWS に触れない）。
lab/lab_topology.py が lab の定義（wanlab.clab.yml.in + frr/*.conf）から作る機器と回線が agent/data の静的データと同じであること
（PyYAML があるときと無いときの両方）、graph/status_handler.py が AnomalyOpened / AnomalyResolved を graph.set_status に正しく写すこと、
terraform/pipeline/graph の sync.tf がその配線を持つこと。実行は uv run --group dev python tests/test_sync.py"""
import builtins, importlib.util, json, os, re, sys, types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "agent"))
passed = 0


def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


# ---- lab → トポロジ
lt = load("lab/lab_topology.py", "lab_topology")
topology = load("agent/topology.py", "topology")   # graph は未配備（環境変数もパラメータも無い）なので静的データ
static_devices, static_links = topology.load_static()
DEV_KEYS = ("hostname", "site", "role", "asn", "mgmt_ip", "enabled")


def same(devices, links):
    sd = {d["device_id"]: d for d in static_devices}
    if {d["device_id"] for d in devices} != set(sd):
        return False
    for d in devices:
        if any(d.get(k) != sd[d["device_id"]].get(k) for k in DEV_KEYS):
            return False
    key = lambda l: (l["a"], l["a_if"], l["b"], l["b_if"], l["kind"], l.get("role"), l.get("bandwidth_mbps"))
    return sorted(map(key, links)) == sorted(map(key, static_links))


devices, links = lt.load(os.path.join(ROOT, "lab"))
check("lab の定義から 10 台と 10 本", len(devices) == 10 and len(links) == 10)
check("機器（hostname / site / role / asn / mgmt_ip / enabled）が agent/data の静的データと同じ", same(devices, static_links))
check("回線（両端の IF / 種別 / 主副 / 帯域）が agent/data の静的データと同じ", same(static_devices, links))
check("回線は a < b に正規化", all(l["a"] < l["b"] for l in links))
check("snmpd のサイドカーは機器に数えず、相乗り先が監視対象", all(d["enabled"] == (d["role"] == "ce") for d in devices))
check("帯域は FRR の description の 1G / 100M / 10G から", lt.bandwidth_mbps("core 10G") == 10000 and lt.bandwidth_mbps("WAN 100M") == 100
      and lt.bandwidth_mbps("LAN") is None and lt.bandwidth_mbps("to pe-01 2.5G") == 2500)
check("主副は description の primary / secondary から", lt.link_role("WAN secondary to x") == "secondary" and lt.link_role("hq-ce-01 primary access") == "primary" and lt.link_role("LAN") is None)
check("FRR の設定から asn と interface の description", lt.parse_frr("frr version 10\ninterface eth1\n description a 1G\n!\nrouter bgp 65001\n neighbor x remote-as 65000\n")
      == {"asn": 65001, "interfaces": {"eth1": {"description": "a 1G"}}})

# PyYAML が無い PC（ops/up.sh を打つ手元の python3）でも同じになる
real_import = builtins.__import__
def no_yaml(name, *a, **k):
    if name == "yaml":
        raise ImportError("no yaml")
    return real_import(name, *a, **k)
builtins.__import__ = no_yaml
try:
    d2, l2 = lt.load(os.path.join(ROOT, "lab"))
finally:
    builtins.__import__ = real_import
hq = next(d for d in devices if d["device_id"] == "hq-ce-01")
check("インタフェースはリンクの両端だけでなく全部（管理の eth0 も、FRR / exec のアドレス付き）",
      [(i["name"], i["address"]) for i in hq["interfaces"]][:2] == [("eth0", "203.0.113.11"), ("eth1", "172.16.1.2")]
      and all({"name", "address"} <= set(i) for d in devices for i in d["interfaces"])
      and all(any(i["name"] == (l["a_if"] if l["a"] == d["device_id"] else l["b_if"]) for i in d["interfaces"])
              for l in links for d in devices if d["device_id"] in (l["a"], l["b"])))
check("別名は device_id / hostname / 管理 IP / 全インタフェースのアドレスを小文字で", {"hq-ce-01", "203.0.113.11", "172.16.1.2"} <= set(hq["aliases"])
      and all(a == a.lower() for d in devices for a in d["aliases"]))
dm = lt.parse_device_map(lt.device_map(devices)) if hasattr(lt, "parse_device_map") else dict(x.split("=", 1) for x in lt.device_map(devices).split(","))
check("device map は別名 → device_id（device_id 自身は省く）で、全機器の管理 IP を含む",
      dm["172.16.1.2"] == "hq-ce-01" and "hq-ce-01" not in dm and all(dm.get(d["mgmt_ip"]) == d["device_id"] for d in devices if d["mgmt_ip"]))
try:
    lt.device_map([{"device_id": "a", "aliases": ["10.0.0.1"]}, {"device_id": "b", "aliases": ["10.0.0.1"]}])
    dup = False
except ValueError:
    dup = True
check("1 つの別名が 2 台を指していたら device map を作らずに止める", dup)
check("snmp agents は監視対象（enabled）の管理 IP だけ", lt.snmp_agents(devices).count("udp://") == sum(1 for d in devices if d["enabled"])
      and lt.snmp_agents([{"enabled": True, "mgmt_ip": "203.0.113.9"}, {"enabled": False, "mgmt_ip": "203.0.113.8"}]) == '"udp://203.0.113.9:161"')
check("FRR の hostname と ip address も読む", lt.parse_frr("hostname R1\ninterface eth1\n ip address 10.0.0.1/30\n!\n")
      == {"asn": None, "hostname": "R1", "interfaces": {"eth1": {"address": "10.0.0.1"}}})
check("PyYAML が無くても同じ結果（自前の読み取り）", d2 == devices and l2 == links)
check("自前の YAML 読み取りはコメント・引用符・真偽値・数値・flow list を読む",
      lt.load_yaml('a: "x # y"  # c\nb: [p, "q"]\nc:\n  - d: 1\n    e: true\n  - f\n') == {"a": "x # y", "b": ["p", "q"], "c": [{"d": 1, "e": True}, "f"]})
check("CLI は --device-map / --snmp-agents をどちらか 1 つ受ける", '"--device-map", "--snmp-agents"' in read("lab", "lab_topology.py"))
check("CLI は {devices, links} の JSON を出す", "json.dump" in read("lab", "lab_topology.py") and '"devices": devices, "links": links' in read("lab", "lab_topology.py"))

# ---- ops/up.sh 7-3b と ops/sync-graph.sh は lab から作って base64 で渡す
up = read("ops", "up.sh"); sync = read("ops", "sync-graph.sh"); seed = read("ops", "seed_graph.py")
check("up.sh 7-3b は lab/lab_topology.py の出力を LAB_TOPOLOGY_B64 で seed_graph.py に渡す", "lab/lab_topology.py lab | base64" in up and "LAB_TOPOLOGY_B64=$LAB_TOPOLOGY_B64 /usr/bin/python3.13 -" in up)
check("sync-graph.sh は --replace で GRAPH_REPLACE=1、--dry-run は Neptune に触らない", "GRAPH_REPLACE=${REPLACE:-0}" in sync and "--replace) REPLACE=1" in sync and 'if [ -n "$DRY" ]; then printf' in sync)
check("seed_graph.py は LAB_TOPOLOGY_B64 を読み、GRAPH_REPLACE=1 のときだけ入れ直す", 'os.environ.get("LAB_TOPOLOGY_B64")' in seed and 'os.environ.get("GRAPH_REPLACE") != "1"' in seed)

# ---- status Lambda（graph.set_status を差し替えて呼び出しを見る）
calls = []
fake_graph = types.ModuleType("graph")
fake_graph.set_status = lambda dev, ifn="", status="DOWN": (calls.append((dev, ifn, status)) or {"updated": 1})
sys.modules["graph"] = fake_graph
h = load("graph/status_handler.py", "status_handler")
ev = lambda t, **d: {"detail-type": t, "source": "demo-poc.spark", "detail": d}
h.handler(ev("AnomalyOpened", anomaly_id="hq-ce-01#link_down#eth1", device_id="hq-ce-01", kind="link_down", target="eth1"))
check("AnomalyOpened の link_down は機器の IF の回線を DOWN", calls[-1] == ("hq-ce-01", "eth1", "DOWN"))
h.handler(ev("AnomalyResolved", anomaly_id="hq-ce-01#link_down#eth1", device_id="hq-ce-01", kind="link_down", target="eth1"))
check("AnomalyResolved は同じ回線を UP", calls[-1] == ("hq-ce-01", "eth1", "UP"))
h.handler(ev("AnomalyOpened", device_id="hq-ce-01", kind="trap", target=".1.3.6.1.6.3.1.1.5.1"))
check("それ以外の trap は機器を ALARM", calls[-1] == ("hq-ce-01", "", "ALARM"))
h.handler(ev("AnomalyResolved", device_id="hq-ce-01", kind="trap", target="x"))
check("trap の解消は機器を UP", calls[-1] == ("hq-ce-01", "", "UP"))
h.handler(ev("AnomalyOpened", device_id="hq-ce-01", kind="link_down", target="?"))
check("IF が分からない linkDown は機器に付ける", calls[-1] == ("hq-ce-01", "", "DOWN"))
n = len(calls)
check("機器が無い・知らない detail-type は何もしない", "ignored" in h.handler(ev("AnomalyOpened", device_id="?", kind="link_down", target="eth1"))
      and "ignored" in h.handler(ev("Other", device_id="hq-ce-01")) and len(calls) == n)
import logging
class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(); self.records = []
    def emit(self, record):
        self.records.append(record)
cap = _Cap(); h.log.addHandler(cap)
fake_graph.set_status = lambda dev, ifn="", status="DOWN": (calls.append((dev, ifn, status)) or {"updated": 0, "unregistered": True})
h.handler(ev("AnomalyOpened", device_id="zz-ce-09", kind="link_down", target="eth1"))
check("未登録の機器・IF の異常は WARNING で UNREGISTERED をログに出す", calls[-1] == ("zz-ce-09", "eth1", "DOWN")
      and cap.records[-1].levelno == logging.WARNING and "UNREGISTERED" in cap.records[-1].getMessage())
fake_graph.set_status = lambda dev, ifn="", status="DOWN": (calls.append((dev, ifn, status)) or {"updated": 1})
h.handler(ev("AnomalyResolved", device_id="hq-ce-01", kind="link_down", target="eth1"))
check("登録済みなら INFO", cap.records[-1].levelno == logging.INFO)
h.log.removeHandler(cap)
check("detail が JSON 文字列でも読む", h.handler({"detail-type": "AnomalyOpened", "detail": json.dumps({"device_id": "dc-ce-01", "kind": "link_down", "target": "eth2"})})
      == {"updated": 1} and calls[-1] == ("dc-ce-01", "eth2", "DOWN"))

# ---- terraform/pipeline/graph の配線
tf = read("terraform", "pipeline", "graph", "sync.tf")
check("sync.tf は status_handler.py を index.py、agent/graph.py を graph.py で zip にする", 'graph/status_handler.py")' in tf and 'filename = "index.py"' in tf and 'agent/graph.py")' in tf and 'filename = "graph.py"' in tf)
# zip に入れ忘れても apply も plan も通り、実行時に ModuleNotFoundError で初めて分かる。だから「含まれている」ではなく「足りていない
# ものが無い」を見る: graph.py が import する agent/ のモジュール（いまは toolkit）が全部 source に並んでいるか
zipped = set(re.findall(r'filename = "(\w+)\.py"', tf))
needed = {m for m in re.findall(r"^import (\w+)$", read("agent", "graph.py"), re.M) if os.path.exists(os.path.join(ROOT, "agent", m + ".py"))}
check(f"status.zip は graph.py が import する agent/ のモジュールを全部入れる（足りない: {sorted(needed - zipped)}）", needed and not (needed - zipped))
check("EventBridge のルールは <接頭辞>.spark の AnomalyOpened と AnomalyResolved（Source を接頭辞ごとに変えて他の人の異常を拾わない）",
      re.search(r'source\s*=\s*\["\$\{local\.name_prefix\}\.spark"\]', tf) and '"detail-type" = ["AnomalyOpened", "AnomalyResolved"]' in tf)
check("Lambda は VPC の中で NEPTUNE_ENDPOINT を環境変数で持ち、ロググループは retention 付き",
      "vpc_config" in tf and "NEPTUNE_ENDPOINT = " in tf and "retention_in_days = var.log_retention_days" in tf)
check("Lambda の SG は Neptune の 8182 へ出て、Neptune の SG がそこからの 8182 を受ける", 'resource "aws_vpc_security_group_egress_rule" "status_to_neptune"' in tf and 'resource "aws_vpc_security_group_ingress_rule" "neptune_from_status"' in tf)
# property('status', ...) は既存値の削除を伴うので Delete も要る（無いと AccessDenied で検知がトポロジに映らない。2026-09-18 実機）
check("Lambda のロールは neptune-db の Read / Write / Delete（Gremlin だけ、他のサービスは持たない）",
      all(f'"neptune-db:{a}DataViaQuery"' in tf for a in ("Read", "Write", "Delete")) and "neptune-db:*" not in tf)
check("EventBridge から Lambda を呼ぶ permission", 'principal     = "events.amazonaws.com"' in tf and "source_arn    = aws_cloudwatch_event_rule.status.arn" in tf)
check("variables.tf に log_retention_days", 'variable "log_retention_days"' in read("terraform", "pipeline", "graph", "variables.tf"))
print(f"通過 {passed} / 失敗 0")
