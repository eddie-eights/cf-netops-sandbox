"""Neptune へのトポロジ同期の模擬テスト（AWS に触れない）。
lab/lab_topology.py が lab の定義（splab.clab.yml.in + srlinux/*.cli。lab/gen_lab.py が作る）から作る機器・回線・上の層が agent/data の静的データと同じであること
（PyYAML があるときと無いときの両方）、graph/status_handler.py が AnomalyOpened / AnomalyResolved を graph.set_status / set_layer_status に正しく写すこと、
terraform/pipeline/graph の sync.tf がその配線を持つこと。実行は uv run --group dev python tests/test_sync.py"""
import builtins, importlib.util, json, os, re, subprocess, sys, tempfile, types

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


devices, links, layers = lt.load(os.path.join(ROOT, "lab"))
check("lab の定義から 8 台と 12 本（Leaf-SW 2 + Spine 2 + Leaf 2 + VM 2）", len(devices) == 8 and len(links) == 12)
check("機器（hostname / site / role / asn / mgmt_ip / enabled）が agent/data の静的データと同じ", same(devices, static_links))
check("回線（両端の IF / 種別 / 主副 / 帯域）が agent/data の静的データと同じ", same(static_devices, links))
check("回線は a < b に正規化", all(l["a"] < l["b"] for l in links))
check("監視対象は SNMP（trap-group）の設定を持つ SR Linux の 6 台（VM は対象外）", all(d["enabled"] == (d["role"] in ("leafsw", "spine", "leaf")) for d in devices) and sum(d["enabled"] for d in devices) == 6)
check("帯域は SR Linux の interface description の 1G / 100M / 10G から", lt.bandwidth_mbps("core 10G") == 10000 and lt.bandwidth_mbps("WAN 100M") == 100
      and lt.bandwidth_mbps("LAN") is None and lt.bandwidth_mbps("to pe-01 2.5G") == 2500 and lt.bandwidth_mbps("fabric to dc1-spine-01 25G") == 25000)
check("主副は description の primary / secondary から", lt.link_role("WAN secondary to x") == "secondary" and lt.link_role("dc1-leaf-01 primary access") == "primary" and lt.link_role("LAN") is None)
check("SR Linux の設定（set / の行）から asn と interface の description、SNMP の有無",
      lt.parse_srl('set / interface ethernet-1/1 description "a 1G"\nset / interface ethernet-1/1 admin-state enable\n'
                   'set / network-instance default protocols bgp autonomous-system 65001\nset / system snmp trap-group t admin-state enable\n')
      == {"asn": 65001, "interfaces": {"ethernet-1/1": {"description": "a 1G"}}, "snmp": True, "subinterfaces": {},
          "isis": {"instance": None, "interfaces": {}}, "bgp": {"router_id": None, "groups": {}, "neighbors": {}}, "evpn": {}, "vxlan": {}, "es": {}})

# PyYAML が無い PC（ops/up.sh を打つ手元の python3）でも同じになる
real_import = builtins.__import__
def no_yaml(name, *a, **k):
    if name == "yaml":
        raise ImportError("no yaml")
    return real_import(name, *a, **k)
builtins.__import__ = no_yaml
try:
    d2, l2, y2 = lt.load(os.path.join(ROOT, "lab"))
finally:
    builtins.__import__ = real_import
leaf = next(d for d in devices if d["device_id"] == "dc1-leaf-01")
check("インタフェースはリンクの両端だけでなく全部（管理の mgmt0 が先頭、SR Linux / exec のアドレス付き）",
      [(i["name"], i["address"]) for i in leaf["interfaces"]][:2] == [("mgmt0", "203.0.113.31"), ("ethernet-1/1", "172.16.0.5")]
      and all({"name", "address"} <= set(i) for d in devices for i in d["interfaces"])
      and all(any(i["name"] == (l["a_if"] if l["a"] == d["device_id"] else l["b_if"]) for i in d["interfaces"])
              for l in links for d in devices if d["device_id"] in (l["a"], l["b"])))
check("別名は device_id / hostname / 管理 IP / 全インタフェースのアドレスを小文字で", {"dc1-leaf-01", "203.0.113.31", "172.16.0.5"} <= set(leaf["aliases"])
      and all(a == a.lower() for d in devices for a in d["aliases"]))
dm = lt.parse_device_map(lt.device_map(devices)) if hasattr(lt, "parse_device_map") else dict(x.split("=", 1) for x in lt.device_map(devices).split(","))
check("device map は別名 → device_id（device_id 自身は省く）で、全機器の管理 IP を含む",
      dm["172.16.0.5"] == "dc1-leaf-01" and "dc1-leaf-01" not in dm and all(dm.get(d["mgmt_ip"]) == d["device_id"] for d in devices if d["mgmt_ip"]))
try:
    lt.device_map([{"device_id": "a", "aliases": ["10.0.0.1"]}, {"device_id": "b", "aliases": ["10.0.0.1"]}])
    dup = False
except ValueError:
    dup = True
check("1 つの別名が 2 台を指していたら device map を作らずに止める", dup)
check("snmp agents は監視対象（enabled）の管理 IP だけ", lt.snmp_agents(devices).count("udp://") == sum(1 for d in devices if d["enabled"])
      and lt.snmp_agents([{"enabled": True, "mgmt_ip": "203.0.113.9"}, {"enabled": False, "mgmt_ip": "203.0.113.8"}]) == '"udp://203.0.113.9:161"')
check("SR Linux の host-name と subinterface の ipv4 address も読む（SNMP 無しなら snmp は False）",
      lt.parse_srl("set / system name host-name R1\nset / interface ethernet-1/1 subinterface 0 ipv4 address 10.0.0.1/30\n")
      == {"asn": None, "hostname": "R1", "interfaces": {"ethernet-1/1": {"address": "10.0.0.1"}}, "snmp": False,
          "subinterfaces": {"ethernet-1/1.0": {"interface": "ethernet-1/1", "address": "10.0.0.1", "prefix_length": 30, "network_instance": None}},
          "isis": {"instance": None, "interfaces": {}}, "bgp": {"router_id": None, "groups": {}, "neighbors": {}}, "evpn": {}, "vxlan": {}, "es": {}})
check("gnmi targets は監視対象（enabled）の管理 IP:57400（Telegraf の inputs.gnmi の addresses）", lt.gnmi_targets(devices).count(":57400") == 6
      and lt.gnmi_targets([{"enabled": True, "mgmt_ip": "203.0.113.9"}, {"enabled": False, "mgmt_ip": "203.0.113.8"}]) == '"203.0.113.9:57400"')
# 上の層（IP 層 / EVPN・BGP 層）。物理層の頂点 <機器>#<IF> を interface_id / ip_interface_id で指す
lv = {v["id"]: v for v in layers["vertices"]}
if_ids = {f'{d["device_id"]}#{i["name"]}' for d in devices for i in d["interfaces"]}
check("上の層は 62 頂点・84 辺で、頂点の id は機器ごとに一意", len(lv) == 62 == len(layers["vertices"]) and len(layers["edges"]) == 84)
check("ip_interface は物理層の interface を interface_id で指し、over の辺でつながる（ループバック system0.0 は物理層に無いので空。IS-IS の隣接は ip_interface_id も）",
      all((v["interface_id"] in if_ids) == (not v["name"].startswith("system0")) for v in lv.values() if v["label"] == "ip_interface")
      and all(v["interface_id"] == "" for v in lv.values() if v["label"] == "ip_interface" and v["name"].startswith("system0"))
      and all(v["ip_interface_id"] in lv and v["interface_id"] in if_ids for v in lv.values() if v["label"] == "isis_adjacency")
      and all(e["to"] in if_ids or e["to"] in lv for e in layers["edges"] if e["label"] == "over"))
check("EVPN・BGP 層は loopback の ip_interface を ip_interface_id で指し、ES は lag の interface を指す",
      all(v["ip_interface_id"].endswith("#system0.0") and v["ip_interface_id"] in lv for v in lv.values() if v["label"] in ("bgp_session", "evpn_instance"))
      and all(v["interface_id"].endswith("#lag1") and v["interface_id"] in if_ids for v in lv.values() if v["label"] == "ethernet_segment"))
check("iBGP EVPN は Leaf-SW / Leaf から Spine 2 台へ（Spine は RR。AS 65100）",
      {(v["device_id"], v["peer_device"]) for v in lv.values() if v["label"] == "bgp_session" and v["role"] == "client"}
      == {(f"dc1-{r}-0{i}", f"dc1-spine-0{s}") for r in ("leafsw", "leaf") for i in (1, 2) for s in (1, 2)}
      and all(v["asn"] == 65100 == v["peer_as"] for v in lv.values() if v["label"] == "bgp_session"))
check("IS-IS の隣接は fabric の 8 本の両端（peer の辺）", sum(1 for v in lv.values() if v["label"] == "isis_adjacency") == 16
      and sum(1 for e in layers["edges"] if e["label"] == "peer" and e["from"].split("#")[1] == "isis") == 8)
check("EVI 100 は 4 台で同じ RT / VNI、ES は Leaf-SW の組と Leaf の組で同じ ESI",
      {(v["evi"], v["vni"], v["route_target"]) for v in lv.values() if v["label"] == "evpn_instance"} == {(100, 100, "target:65100:100")}
      and len({v["esi"] for v in lv.values() if v["label"] == "ethernet_segment" and v["device_id"].startswith("dc1-leaf-")}) == 1
      and len({v["esi"] for v in lv.values() if v["label"] == "ethernet_segment"}) == 2)
check("agent/data/layers.json は lab の定義から作った上の層と同じ", topology.load_static_layers() == layers)
# lab の定義は lab/gen_lab.py の出力そのもの（手で直さない。台数を変えるときは gen_lab.py を回す）
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run([sys.executable, os.path.join(ROOT, "lab", "gen_lab.py"), "--out", tmp], check=True, capture_output=True)
    gen = {}
    for dp, _, fns in os.walk(tmp):
        for fn in fns:
            p = os.path.join(dp, fn)
            gen[os.path.relpath(p, tmp)] = open(p, encoding="utf-8").read()
check("lab/splab.clab.yml.in と lab/srlinux/*.cli は lab/gen_lab.py の出力と同じ（既定の leaf 2・spine 2）",
      set(gen) == {"splab.clab.yml.in"} | {f"srlinux/{n}.cli" for n in ("dc1-leafsw-01", "dc1-leafsw-02", "dc1-spine-01", "dc1-spine-02", "dc1-leaf-01", "dc1-leaf-02")}
      and all(read("lab", *rel.split("/")) == text for rel, text in gen.items()))
check("PyYAML が無くても同じ結果（自前の読み取り）", d2 == devices and l2 == links and y2 == layers)
check("自前の YAML 読み取りはコメント・引用符・真偽値・数値・flow list を読む",
      lt.load_yaml('a: "x # y"  # c\nb: [p, "q"]\nc:\n  - d: 1\n    e: true\n  - f\n') == {"a": "x # y", "b": ["p", "q"], "c": [{"d": 1, "e": True}, "f"]})
check("CLI は --device-map / --snmp-agents / --gnmi-targets / --layers を受ける", '"--device-map", "--snmp-agents", "--gnmi-targets", "--layers"' in read("lab", "lab_topology.py"))
check("CLI は {devices, links, layers} の JSON を出す", "json.dump" in read("lab", "lab_topology.py") and '"devices": devices, "links": links, "layers": lyr' in read("lab", "lab_topology.py"))

# ---- ops/up.sh 7-3b と ops/sync-graph.sh は lab から作って base64 で渡す
up = read("ops", "up.sh"); sync = read("ops", "sync-graph.sh"); seed = read("ops", "seed_graph.py")
check("up.sh 7-3b は lab/lab_topology.py の出力を LAB_TOPOLOGY_B64 で seed_graph.py に渡す", "lab/lab_topology.py lab | base64" in up and "LAB_TOPOLOGY_B64=$LAB_TOPOLOGY_B64 /usr/bin/python3.13 -" in up)
check("sync-graph.sh は --replace で GRAPH_REPLACE=1、--dry-run は Neptune に触らない", "GRAPH_REPLACE=${REPLACE:-0}" in sync and "--replace) REPLACE=1" in sync and 'if [ -n "$DRY" ]; then printf' in sync)
check("seed_graph.py は LAB_TOPOLOGY_B64 を読み、GRAPH_REPLACE=1 のときだけ入れ直す", 'os.environ.get("LAB_TOPOLOGY_B64")' in seed and 'os.environ.get("GRAPH_REPLACE") != "1"' in seed)
check("seed_graph.py は layers も渡す（lab からは JSON の layers、静的データは data/layers.json）", 'lab.get("layers")' in seed and "topology.load_static_layers()" in seed and "graph.seed(devices, links, layers)" in seed)
check("up.sh は gNMI の購読先も lab の定義から作って stream の gnmi_targets に渡し、gateway.tf は data/layers.json を tools の zip に入れる",
      "lab/lab_topology.py lab --gnmi-targets" in up and '-var "gnmi_targets=$GNMI_TARGETS"' in up
      and '"../../agent/data/layers.json"' in read("terraform", "workflow", "gateway.tf"))

# ---- status Lambda（graph.set_status を差し替えて呼び出しを見る）
calls = []
fake_graph = types.ModuleType("graph")
fake_graph.set_status = lambda dev, ifn="", status="DOWN", only_if="": (calls.append((dev, ifn, status) + ((only_if,) if only_if else ())) or {"updated": 1})
layer_calls = []
fake_graph.set_layer_status = lambda dev, kind, target, status="DOWN": (layer_calls.append((dev, kind, target, status)) or {"updated": 1})
sys.modules["graph"] = fake_graph
h = load("graph/status_handler.py", "status_handler")
ev = lambda t, **d: {"detail-type": t, "source": "demo-poc.spark", "detail": d}
h.handler(ev("AnomalyOpened", anomaly_id="dc1-leaf-01#link_down#eth1", device_id="dc1-leaf-01", kind="link_down", target="eth1"))
check("AnomalyOpened の link_down は機器の IF の回線を DOWN", calls[-1] == ("dc1-leaf-01", "eth1", "DOWN"))
h.handler(ev("AnomalyResolved", anomaly_id="dc1-leaf-01#link_down#eth1", device_id="dc1-leaf-01", kind="link_down", target="eth1"))
check("AnomalyResolved は同じ回線を UP", calls[-1] == ("dc1-leaf-01", "eth1", "UP"))
h.handler(ev("AnomalyOpened", device_id="dc1-leaf-01", kind="trap", target=".1.3.6.1.6.3.1.1.5.1"))
check("それ以外の trap は機器を ALARM", calls[-1] == ("dc1-leaf-01", "", "ALARM"))
h.handler(ev("AnomalyResolved", device_id="dc1-leaf-01", kind="trap", target="x"))
check("trap の解消は機器が ALARM のときだけ UP（linkDown の DOWN は上書きしない）", calls[-1] == ("dc1-leaf-01", "", "UP", "ALARM"))
h.handler(ev("AnomalyOpened", device_id="dc1-leaf-01", kind="link_down", target="?"))
check("IF が分からない linkDown は機器に付ける", calls[-1] == ("dc1-leaf-01", "", "DOWN"))
n = len(calls)
h.handler(ev("AnomalyOpened", anomaly_id="dc1-leaf-01#bgp_down#10.255.0.1", device_id="dc1-leaf-01", kind="bgp_down", target="10.255.0.1"))
check("bgp_down（gNMI）は BGP のセッションの頂点を set_layer_status で DOWN（set_status は呼ばない）", layer_calls[-1] == ("dc1-leaf-01", "bgp", "10.255.0.1", "DOWN") and len(calls) == n)
h.handler(ev("AnomalyResolved", device_id="dc1-leaf-01", kind="bgp_down", target="10.255.0.1"))
check("bgp_down の解消は同じ頂点を UP", layer_calls[-1] == ("dc1-leaf-01", "bgp", "10.255.0.1", "UP"))
h.handler(ev("AnomalyOpened", device_id="dc1-leaf-01", kind="isis_down", target="ethernet-1/1.0"))
check("isis_down は IS-IS の隣接の頂点（target = サブインタフェース）", layer_calls[-1] == ("dc1-leaf-01", "isis", "ethernet-1/1.0", "DOWN"))
m = len(layer_calls)
check("target の無い bgp_down / isis_down は何もしない", "ignored" in h.handler(ev("AnomalyOpened", device_id="dc1-leaf-01", kind="isis_down", target="?"))
      and "ignored" in h.handler(ev("AnomalyOpened", device_id="dc1-leaf-01", kind="bgp_down")) and len(layer_calls) == m and len(calls) == n)
n = len(calls)
check("機器が無い・知らない detail-type は何もしない", "ignored" in h.handler(ev("AnomalyOpened", device_id="?", kind="link_down", target="eth1"))
      and "ignored" in h.handler(ev("Other", device_id="dc1-leaf-01")) and len(calls) == n)
import logging
class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(); self.records = []
    def emit(self, record):
        self.records.append(record)
cap = _Cap(); h.log.addHandler(cap)
fake_graph.set_status = lambda dev, ifn="", status="DOWN", only_if="": (calls.append((dev, ifn, status) + ((only_if,) if only_if else ())) or {"updated": 0, "unregistered": True})
h.handler(ev("AnomalyOpened", device_id="zz-ce-09", kind="link_down", target="eth1"))
check("未登録の機器・IF の異常は WARNING で UNREGISTERED をログに出す", calls[-1] == ("zz-ce-09", "eth1", "DOWN")
      and cap.records[-1].levelno == logging.WARNING and "UNREGISTERED" in cap.records[-1].getMessage())
fake_graph.set_status = lambda dev, ifn="", status="DOWN", only_if="": (calls.append((dev, ifn, status) + ((only_if,) if only_if else ())) or {"updated": 1})
h.handler(ev("AnomalyResolved", device_id="dc1-leaf-01", kind="link_down", target="eth1"))
check("登録済みなら INFO", cap.records[-1].levelno == logging.INFO)
h.log.removeHandler(cap)
check("detail が JSON 文字列でも読む", h.handler({"detail-type": "AnomalyOpened", "detail": json.dumps({"device_id": "dc1-leaf-02", "kind": "link_down", "target": "eth2"})})
      == {"updated": 1} and calls[-1] == ("dc1-leaf-02", "eth2", "DOWN"))

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
check("Lambda と Neptune は base/core の internal SG 1 つに相乗り（2026-09-26 に SG を 12 個から 2 個にした。専用の SG と 8182 の rule は無い）",
      "security_group_ids = [local.internal_sg_id]" in tf and "aws_vpc_security_group_egress_rule" not in tf and "aws_vpc_security_group_ingress_rule" not in tf
      and "vpc_security_group_ids              = [local.internal_sg_id]" in read("terraform", "pipeline", "graph", "neptune.tf"))
# property('status', ...) は既存値の削除を伴うので Delete も要る（無いと AccessDenied で検知がトポロジに映らない。2026-09-18 実機）
check("Lambda のロールは neptune-db の Read / Write / Delete（Gremlin だけ、他のサービスは持たない）",
      all(f'"neptune-db:{a}DataViaQuery"' in tf for a in ("Read", "Write", "Delete")) and "neptune-db:*" not in tf)
check("EventBridge から Lambda を呼ぶ permission", 'principal     = "events.amazonaws.com"' in tf and "source_arn    = aws_cloudwatch_event_rule.status.arn" in tf)
check("variables.tf に log_retention_days", 'variable "log_retention_days"' in read("terraform", "pipeline", "graph", "variables.tf"))
print(f"通過 {passed} / 失敗 0")
