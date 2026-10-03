"""agent/graph.py と topology.py の Neptune 経路の模擬テスト。boto3 を差し替え、送った Gremlin と読み替えを確かめる。
実行は python3 tests/test_graph.py（PyYAML が要る）。"""
import importlib.util, os, sys, types

AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent")
sys.path.insert(0, AGENT)

class ClientError(Exception):
    pass
class BotoCoreError(Exception):
    pass

state = {"queries": [], "answer": {}, "fail": False, "ssm": {}}

def gs(v):
    """GraphSON 3 風の包み"""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return {"@type": "g:Int64", "@value": v}
    if isinstance(v, list):
        return {"@type": "g:List", "@value": [gs(x) for x in v]}
    if isinstance(v, dict):
        flat = []
        for k, x in v.items():
            flat += [gs(k), gs(x)]
        return {"@type": "g:Map", "@value": flat}
    return v

class FakeClient:
    def __init__(self, name, **kw):
        self.name = name
        self.kw = kw
    def get_parameter(self, Name):
        if Name in state["ssm"]:
            return {"Parameter": {"Value": state["ssm"][Name]}}
        raise ClientError("ParameterNotFound")
    def execute_gremlin_query(self, gremlinQuery):
        state["queries"].append(gremlinQuery)
        if state["fail"]:
            raise BotoCoreError("boom")
        for key, val in state["answer"].items():
            if key in gremlinQuery:
                return {"requestId": "r", "status": {"code": 200}, "result": {"data": gs(val)}}
        return {"result": {"data": gs([0])}}

boto3 = types.ModuleType("boto3"); boto3.client = lambda name, **kw: FakeClient(name, **kw)
botocore = types.ModuleType("botocore"); exc = types.ModuleType("botocore.exceptions")
exc.ClientError = ClientError; exc.BotoCoreError = BotoCoreError; botocore.exceptions = exc
cfg = types.ModuleType("botocore.config"); cfg.Config = lambda **kw: kw; botocore.config = cfg
sys.modules.update({"boto3": boto3, "botocore": botocore, "botocore.exceptions": exc, "botocore.config": cfg})

passed = 0
def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)

def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(AGENT, f"{name}.py"))
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m

# ---- 未配備
os.environ.pop("NEPTUNE_ENDPOINT", None); os.environ["PARAM_PREFIX"] = "/x"; os.environ["TOPOLOGY_TTL"] = "0"
graph = load("graph")
check("SSM に無ければ未配備", not graph.configured())
topology = load("topology")
check("未配備なら静的データ", topology.SOURCE == "static" and len(topology.DEVICES) == 8)

# ---- GraphSON の読み替え
check("_un は List / Map / Int を素の値に", graph._un(gs({"a": [1, 2], "b": "s"})) == {"a": [1, 2], "b": "s"})
check("_q は文字列を ' で囲み ' をエスケープ", graph._q("it's") == "'it\\'s'" and graph._q(5) == "5" and graph._q(True) == "true")

# ---- 配備あり（環境変数）
os.environ["NEPTUNE_ENDPOINT"] = "db.example:8182"
graph = load("graph")
check("環境変数で配備あり", graph.configured() and graph._client().kw["endpoint_url"] == "https://db.example:8182")
devs = [{"id": "a-ce-01", "label": "device", "hostname": "a-ce-01", "site": "a", "role": "leaf", "asn": 65001, "mgmt_ip": "203.0.113.11", "enabled": True},
        {"id": "b-ce-01", "label": "device", "hostname": "b-ce-01", "site": "b", "role": "leaf", "asn": None, "mgmt_ip": "203.0.113.12", "enabled": False}]
ifs = [{"id": "a-ce-01#eth0", "label": "interface", "device_id": "a-ce-01", "name": "eth0", "address": "203.0.113.11"},
       {"id": "a-ce-01#eth1", "label": "interface", "device_id": "a-ce-01", "name": "eth1", "address": "172.16.1.2", "status": "DOWN"},
       {"id": "gone#eth1", "label": "interface", "device_id": "gone", "name": "eth1"}]
links = [{"id": "e1", "label": "link", "OUT": {"id": "a-ce-01"}, "IN": {"id": "b-ce-01"}, "a_if": "eth1", "b_if": "eth1", "kind": "l2", "role": "primary", "bandwidth_mbps": 1000}]
lv = [{"id": "a-ce-01#eth1.0", "label": "ip_interface", "layer": "ip", "device_id": "a-ce-01", "interface_id": "a-ce-01#eth1", "name": "eth1.0", "address": "172.16.1.2"},
      {"id": "a-ce-01#bgp#10.255.0.9", "label": "bgp_session", "layer": "evpn", "device_id": "a-ce-01", "peer_address": "10.255.0.9", "status": "DOWN", "registered": False}]
le = [{"id": "le1", "label": "over", "OUT": {"id": "a-ce-01#eth1.0"}, "IN": {"id": "a-ce-01#eth1"}}]
state.update(answer={"g.V().hasLabel('device').elementMap()": devs, "g.V().hasLabel('interface').elementMap()": ifs,
                     "g.E().hasLabel('link').elementMap()": links, "g.V().hasLabel('ip_interface','isis_adjacency','bgp_session','evpn_instance','ethernet_segment').elementMap()": lv,
                     "g.E().hasLabel('over','peer','tunnel','attach','segment').elementMap()": le}, queries=[])
d, l = graph.load_topology()
y = graph.load_layers()
check("load_layers は上の層の頂点（registered を付ける。未登録は False）と辺（from / to は両端の id）を id の順で返す",
      [v["id"] for v in y["vertices"]] == ["a-ce-01#bgp#10.255.0.9", "a-ce-01#eth1.0"] and y["vertices"][0]["registered"] is False and y["vertices"][0]["status"] == "DOWN"
      and y["vertices"][1]["registered"] is True and y["edges"] == [{"label": "over", "from": "a-ce-01#eth1.0", "to": "a-ce-01#eth1"}])
check("load_topology はインタフェースの頂点を device_id で機器に付け、機器の無いものは捨てる",
      d[0]["interfaces"] == [{"name": "eth0", "address": "203.0.113.11", "lag": "", "status": None, "registered": True},
                             {"name": "eth1", "address": "172.16.1.2", "lag": "", "status": "DOWN", "registered": True}]
      and d[1]["interfaces"] == [] and d[0]["registered"] is True)
check("load_topology は device_id と a/b を組み立てる", d[0]["device_id"] == "a-ce-01" and d[0]["asn"] == 65001 and d[1]["enabled"] is False and l == [{"a_if": "eth1", "b_if": "eth1", "kind": "l2", "role": "primary", "bandwidth_mbps": 1000, "status": None, "a": "a-ce-01", "b": "b-ce-01"}])

topology = load("topology")
check("配備ありなら Neptune から組む", topology.SOURCE == "neptune" and [x["device_id"] for x in topology.DEVICES] == ["a-ce-01", "b-ce-01"])
check("隣接も Neptune の辺から", topology.neighbors("a-ce-01")["neighbors"][0]["device_id"] == "b-ce-01")
check("topology_graph の source は neptune", topology.topology_graph()["source"] == "neptune")

# ---- 読めなければ静的へ
state["fail"] = True
check("Neptune が落ちていれば静的データに戻る", topology.reload(force=True) == "static" and len(topology.DEVICES) == 8)
state["fail"] = False
state.update(answer={"g.V().hasLabel('device').elementMap()": [], "g.V().hasLabel('interface').elementMap()": [], "g.E().hasLabel('link').elementMap()": []})
check("Neptune が空なら静的データを見せて neptune-empty", topology.reload(force=True) == "neptune-empty" and len(topology.DEVICES) == 8)

# ---- 書き込みの Gremlin
state.update(answer={"count()": [1]}, queries=[])
check("add_link は同じ機器を拒む", "error" in graph.add_link("a", "e1", "a", "e2"))
state.update(answer={"outE('link')": [0], "count()": [1]}, queries=[])
r = graph.add_link("b-ce-01", "eth2", "a-ce-01", "eth3", "fabric", "secondary", 100)
q = state["queries"][-1]
check("add_link は a < b に正規化して addE", r.get("added") == "a-ce-01 eth3 - b-ce-01 eth2" and q.startswith("g.addE('link').from(__.V('a-ce-01')).to(__.V('b-ce-01'))") and ".property('bandwidth_mbps',100)" in q and ".property('role','secondary')" in q)
state.update(answer={"count()": [0]}, queries=[])
check("機器が無ければ add_link は error", "機器が無い" in graph.add_link("x", "e", "y", "e")["error"])
state.update(answer={"count()": [1]}, queries=[])
check("remove_link は drop を送る", graph.remove_link("b-ce-01", "a-ce-01", "eth3") == {"removed": 1} and state["queries"][-1] == "g.V('a-ce-01').outE('link').where(inV().hasId('b-ce-01')).has('a_if','eth3').drop()")
state.update(answer={"coalesce(": [], "count()": [0]}, queries=[])
check("add_device は property を並べる", graph.add_device("c-ce-01", "c", "ce", "203.0.113.15", 65003) == {"added": "c-ce-01"} and ".property('asn',65003)" in state["queries"][-1] and ".property('enabled',false)" in state["queries"][-1])
check("remove_device は無ければ error", "error" in graph.remove_device("zzz"))
state.update(answer={"count()": [1]}, queries=[])
check("remove_device はインタフェースの頂点も消す（辺でつないでいない）", graph.remove_device("a-ce-01") == {"removed": "a-ce-01"}
      and state["queries"][1:] == ["g.V('a-ce-01').drop()", "g.V().hasLabel('interface').has('device_id','a-ce-01').drop()"])
state.update(answer={"coalesce(": [True]}, queries=[])
check("add_device は登録済みなら error", "error" in graph.add_device("a-ce-01", "a", "ce"))
state.update(answer={"coalesce(": [False], "values('status')": ["ALARM"]}, queries=[])
r = graph.add_device("zz-ce-09", "zz", "ce")
check("add_device は未登録の頂点を置き換え、status を引き継ぐ", r == {"added": "zz-ce-09", "replaced_unregistered": True}
      and state["queries"][2] == "g.V('zz-ce-09').drop()" and ".property('status','ALARM')" in state["queries"][3]
      and state["queries"][3].startswith("g.addV('device').property(id,'zz-ce-09')"))
state.update(answer={"count()": [3]}, queries=[])
check("count は登録済みの機器・IF・回線、上の層の頂点と辺、未登録の頂点を数える", graph.count() == {"devices": 3, "interfaces": 3, "links": 3, "layers": 3, "layer_edges": 3, "unregistered": 3}
      and state["queries"][0] == "g.V().hasLabel('device').hasNot('registered').count()" and state["queries"][5] == "g.V().has('registered',false).count()"
      and state["queries"][3].startswith("g.V().hasLabel('ip_interface',") and "'bgp_session'" in state["queries"][3])
# 判定は辞書の順（outE の count は 0 = リンク未登録、機器の count は 1 = 登録済み）
spec = importlib.util.spec_from_file_location("lab_topology", os.path.join(AGENT, "..", "lab", "lab_topology.py"))
lt = importlib.util.module_from_spec(spec); spec.loader.exec_module(lt)
lab_devices, lab_links, lab_layers = lt.load(os.path.join(AGENT, "..", "lab"))
n_if = sum(len(x["interfaces"]) for x in lab_devices)
placeholders = [{"id": "dc1-leaf-01", "label": "device", "registered": False, "role": "unknown", "status": "ALARM"},
                {"id": "dc1-leaf-01#ethernet-1/1", "label": "interface", "registered": False, "device_id": "dc1-leaf-01", "name": "ethernet-1/1", "status": "DOWN"},
                {"id": "zz-ce-09", "label": "device", "registered": False, "role": "unknown", "status": "ALARM"}]
state.update(answer={"has('registered',false).elementMap()": placeholders, "outE('link')": [0], "inE('link')": [0], "drop()": [], "addV": [], "addE": [], "count()": [1]}, queries=[])
r = graph.seed(lab_devices, lab_links)
qs = state["queries"]
check("seed は未登録の頂点を読み、登録済みを drop してから、今回登録される未登録の頂点だけ drop する",
      qs[0] == "g.V().has('registered',false).elementMap()" and qs[1] == "g.V().hasLabel('device','interface').hasNot('registered').drop()"
      and qs[2:4] == ["g.V('dc1-leaf-01').drop()", "g.V('dc1-leaf-01#ethernet-1/1').drop()"] and not any("'zz-ce-09'" in q for q in qs))
check(f"seed は lab の 8 台と全インタフェース {n_if} 個を addV、12 本を addE", n_if > 20
      and sum(q.startswith("g.addV('device')") for q in qs) == 8 and sum(q.startswith("g.addV('interface')") for q in qs) == n_if
      and "g.addV('interface').property(id,'dc1-leaf-01#mgmt0').property('device_id','dc1-leaf-01').property('name','mgmt0').property('address','203.0.113.31')" in qs
      and "g.addV('interface').property(id,'dc1-leaf-01#ethernet-1/3').property('device_id','dc1-leaf-01').property('name','ethernet-1/3').property('lag','lag1')" in qs
      and sum(q.startswith("g.addE") for q in qs) == 12 and not any(q.startswith("g.addV('ip_interface')") for q in qs))
check("seed は status を入れず、置き換えた未登録の頂点の UP でない status だけ引き継ぐ",
      [q for q in qs if "'status'" in q] == [
          "g.V('dc1-leaf-01').property(single,'status','ALARM').coalesce(values('registered'),constant(true))",
          "g.V('dc1-leaf-01').outE('link').has('a_if','ethernet-1/1').property('status','DOWN').count()",
          "g.V('dc1-leaf-01').inE('link').has('b_if','ethernet-1/1').property('status','DOWN').count()",
          "g.V('dc1-leaf-01#ethernet-1/1').property(single,'status','DOWN').coalesce(values('registered'),constant(true))"])
state.update(answer={"has('registered',false).elementMap()": [], "outE('link')": [0], "drop()": [], "addV": [], "addE": [], "count()": [1]}, queries=[])
r = graph.seed(*topology.load_static())
check("静的データ（インタフェースの一覧が無い）でも seed は 8 台と 12 本", sum(q.startswith("g.addV('device')") for q in state["queries"]) == 8
      and not any(q.startswith("g.addV('interface')") for q in state["queries"]) and sum(q.startswith("g.addE") for q in state["queries"]) == 12
      and not any("'status'" in q for q in state["queries"]))
# 上の層（IP 層 / EVPN・BGP 層）は layers を渡したときだけ。辺は両端の頂点（物理層の interface も含む）があるものだけ張る
layer_ph = [{"id": "dc1-leaf-01#bgp#10.255.0.1", "label": "bgp_session", "registered": False, "device_id": "dc1-leaf-01", "status": "DOWN"}]
state.update(answer={"has('registered',false).elementMap()": layer_ph, "hasLabel('interface').id()": [], "outE('link')": [0], "drop()": [], "addV": [], "addE": [], "count()": [1]}, queries=[])
r = graph.seed(lab_devices, lab_links, lab_layers)
qs = state["queries"]
n_lv, n_le = len(lab_layers["vertices"]), len(lab_layers["edges"])
check(f"seed に layers を渡すと上の層の {n_lv} 頂点と {n_le} 辺も入れ、未登録の同じ id の頂点を drop して status を引き継ぐ",
      n_lv == 62 and n_le == 84 and sum(q.startswith("g.addV('device')") for q in qs) == 8
      and sum(any(q.startswith(f"g.addV('{lb}')") for lb in graph.LAYER_LABELS) for q in qs) == n_lv
      and sum(q.startswith("g.addE") for q in qs) == 12 + n_le and "g.V('dc1-leaf-01#bgp#10.255.0.1').drop()" in qs
      and qs.index("g.V('dc1-leaf-01#bgp#10.255.0.1').property(single,'status','DOWN')") > max(i for i, q in enumerate(qs) if q.startswith("g.add"))
      and r.get("layers") == 1 and r.get("layer_edges") == 1 and "skipped_edges" not in r)
check("上の層の頂点は属性を全部 property に、id と label は付けない",
      "g.addV('bgp_session').property(id,'dc1-leaf-01#bgp#10.255.0.1').property('layer','evpn').property('device_id','dc1-leaf-01').property('peer_address','10.255.0.1')" in " ".join(qs)
      and not any(".property('label'" in q or ".property('id'" in q for q in qs))
state.update(answer={"has('registered',false).elementMap()": [], "hasLabel('interface').id()": [], "outE('link')": [0], "drop()": [], "addV": [], "addE": [], "count()": [1]}, queries=[])
r = graph.seed_layers({"vertices": [{"id": "x#bgp#1", "label": "bgp_session", "device_id": "x"}], "edges": [{"label": "over", "from": "x#bgp#1", "to": "x#eth0.0"}]})
check("seed_layers は片端の無い辺を張らずに skipped_edges に数える", r.get("skipped_edges") == 1 and not any(q.startswith("g.addE") for q in state["queries"]))

# ---- 差分の同期（Nautobot の Job が呼ぶ。status と上の層は触らない）
cur_v = [{"id": "a-ce-01", "label": "device", "hostname": "a-ce-01", "site": "a", "role": "leaf", "asn": 65001, "mgmt_ip": "203.0.113.11", "enabled": True, "status": "DOWN"},
         {"id": "old-ce-01", "label": "device", "hostname": "old-ce-01", "site": "a", "role": "leaf", "enabled": False},
         {"id": "new-ce-01", "label": "device", "registered": False, "role": "unknown", "status": "ALARM"},
         {"id": "zz-ce-09", "label": "device", "registered": False, "role": "unknown", "status": "ALARM"},
         {"id": "a-ce-01#eth1", "label": "interface", "device_id": "a-ce-01", "name": "eth1", "address": "172.16.1.2", "status": "DOWN"},
         {"id": "a-ce-01#eth2", "label": "interface", "device_id": "a-ce-01", "name": "eth2"},
         {"id": "old-ce-01#eth1", "label": "interface", "device_id": "old-ce-01", "name": "eth1"}]
cur_e = [{"id": "e1", "label": "link", "OUT": {"id": "a-ce-01"}, "IN": {"id": "b-ce-01"}, "a_if": "eth1", "b_if": "eth1", "kind": "l2", "role": "primary", "bandwidth_mbps": 1000, "status": "DOWN"},
         {"id": "e2", "label": "link", "OUT": {"id": "a-ce-01"}, "IN": {"id": "b-ce-01"}, "a_if": "eth2", "b_if": "eth2", "kind": "l2"},
         {"id": "e3", "label": "link", "OUT": {"id": "a-ce-01"}, "IN": {"id": "old-ce-01"}, "a_if": "eth3", "b_if": "eth1", "kind": "l2"}]
sync_devs = [{"device_id": "a-ce-01", "hostname": "a-ce-01", "site": "a", "role": "spine", "asn": None, "mgmt_ip": "203.0.113.11", "enabled": True,
              "interfaces": [{"name": "eth1", "address": "172.16.1.2", "lag": ""}, {"name": "eth9", "address": "", "lag": "bond0"}]},
             {"device_id": "new-ce-01", "hostname": "new-ce-01", "site": "a", "role": "leaf", "asn": None, "mgmt_ip": "", "enabled": False, "interfaces": []}]
sync_links = [{"a": "b-ce-01", "a_if": "eth1", "b": "a-ce-01", "b_if": "eth1", "kind": "l2", "role": None, "bandwidth_mbps": 25000},
              {"a": "a-ce-01", "a_if": "eth9", "b": "new-ce-01", "b_if": "eth1", "kind": "fabric", "role": None, "bandwidth_mbps": None}]
state.update(answer={"g.V().hasLabel('device','interface').elementMap()": cur_v, "g.E().hasLabel('link').elementMap()": cur_e,
                     "outE('link')": [0], "inE('link')": [0], "coalesce(": [True], "drop()": [], "addV": [], "addE": [], ".id()": ["x"], "count()": [1]}, queries=[])
r = graph.sync_physical(sync_devs, sync_links)
qs = state["queries"]
check("sync_physical は今の頂点と辺を 1 回ずつ読み、全部を消す drop は送らない",
      qs[:2] == ["g.V().hasLabel('device','interface').elementMap()", "g.E().hasLabel('link').elementMap()"]
      and not any(q.startswith("g.V().hasLabel") and q.endswith(".drop()") for q in qs))
check("一覧に無い登録済みの機器・インタフェースと、一覧に無い回線は消す（未登録の頂点 zz-ce-09 と、機器ごと消えた辺 e3 には触らない）",
      all(q in qs for q in ("g.V('old-ce-01').drop()", "g.V('a-ce-01#eth2').drop()", "g.V('old-ce-01#eth1').drop()", "g.E('e2').drop()"))
      and not any("'zz-ce-09'" in q or "'e3'" in q for q in qs))
check("残る機器は変わった property だけ single で上書きし、無くなった値は property ごと消す（status は書かない）",
      "g.V('a-ce-01').property(single,'role','spine').sideEffect(properties('asn').drop()).id()" in qs
      and not any(q.startswith("g.V('a-ce-01#eth1')") for q in qs))
check("残る回線は a < b に直して突き合わせ、変わった property だけ書く（辺に single は付けない。status は残る）",
      "g.E('e1').sideEffect(properties('role').drop()).property('bandwidth_mbps',25000).id()" in qs)
check("未登録の頂点が一覧にあれば置き換えて UP でない status を引き継ぎ、新しい IF と回線を足す",
      qs.index("g.V('new-ce-01').drop()") < qs.index("g.addV('device').property(id,'new-ce-01').property('hostname','new-ce-01').property('site','a').property('role','leaf').property('enabled',false)")
      and "g.addV('interface').property(id,'a-ce-01#eth9').property('device_id','a-ce-01').property('name','eth9').property('lag','bond0')" in qs
      and "g.addE('link').from(__.V('a-ce-01')).to(__.V('new-ce-01')).property('a_if','eth9').property('b_if','eth1').property('kind','fabric')" in qs
      and qs.index("g.V('new-ce-01').property(single,'status','ALARM').coalesce(values('registered'),constant(true))") > max(i for i, q in enumerate(qs) if q.startswith("g.add")))
check("戻り値は count() に足した数・変えた数・消した数", r["added"] == 3 and r["updated"] == 2 and r["removed"] == 4 and r["devices"] == 1 and "skipped" not in r)
state["queries"] = []
same_v = [{"id": "a-ce-01", "label": "device", "hostname": "a-ce-01", "site": "a", "role": "spine", "mgmt_ip": "203.0.113.11", "enabled": True, "status": "DOWN"},
          {"id": "a-ce-01#eth1", "label": "interface", "device_id": "a-ce-01", "name": "eth1", "address": "172.16.1.2"}]
state["answer"].update({"g.V().hasLabel('device','interface').elementMap()": same_v, "g.E().hasLabel('link').elementMap()": []})
r = graph.sync_physical([{**sync_devs[0], "interfaces": sync_devs[0]["interfaces"][:1]}], [])
check("同じ内容なら何も書かない（読む 2 本と count だけ）", r["added"] == r["updated"] == r["removed"] == 0
      and not any(x in q for q in state["queries"] for x in ("drop()", "addV", "addE", ".property(")))
state.update(answer={"g.V().hasLabel('device','interface').elementMap()": [], "g.E().hasLabel('link').elementMap()": [], "addV": [], "count()": [0]}, queries=[])
r = graph.sync_physical([], [{"a": "x", "a_if": "e", "b": "y", "b_if": "e"}])
check("端の機器が無い回線は張らずに skipped に理由を出す", r["added"] == 0 and len(r["skipped"]) == 1 and "機器が無い" in r["skipped"][0])

# ---- 動的な状態（graph/status_handler.py が呼ぶ）
state.update(answer={"outE('link')": [1], "inE('link')": [0], "coalesce(": [True]}, queries=[])
r = graph.set_status("dc1-leaf-01", "eth1", "down")
check("set_status は IF 付きなら a 側の outE と b 側の inE の辺と、インタフェースの頂点（single）に書き、更新数を返す",
      r == {"device_id": "dc1-leaf-01", "if_name": "eth1", "status": "DOWN", "updated": 2}
      and state["queries"] == ["g.V('dc1-leaf-01').outE('link').has('a_if','eth1').property('status','DOWN').count()",
                               "g.V('dc1-leaf-01').inE('link').has('b_if','eth1').property('status','DOWN').count()",
                               "g.V('dc1-leaf-01#eth1').property(single,'status','DOWN').coalesce(values('registered'),constant(true))"])
state.update(answer={"coalesce(": [True]}, queries=[])
check("set_status は IF 無しなら機器の頂点に single で書く（Neptune の既定の set だと値が積み重なる）",
      graph.set_status("dc1-leaf-01", "", "ALARM") == {"device_id": "dc1-leaf-01", "status": "ALARM", "updated": 1}
      and state["queries"] == ["g.V('dc1-leaf-01').property(single,'status','ALARM').coalesce(values('registered'),constant(true))"])
state.update(answer={"coalesce(": []}, queries=[])
check("only_if を渡すと、今の status がそれのときだけ書き、合わなければ未登録の頂点も作らない",
      graph.set_status("dc1-leaf-01", "", "UP", only_if="ALARM") == {"device_id": "dc1-leaf-01", "status": "UP", "updated": 0}
      and state["queries"] == ["g.V('dc1-leaf-01').has('status','ALARM').property(single,'status','UP').coalesce(values('registered'),constant(true))"])
state.update(answer={"fold()": [], "coalesce(": []}, queries=[])
check("only_if があれば UP 以外でも、合わなければ未登録の頂点を作らない",
      "unregistered" not in graph.set_status("zz-ce-09", "", "ALARM", only_if="DOWN") and not any("addV" in q for q in state["queries"]))
state.update(answer={"coalesce(": [True]}, queries=["x"])
check("set_status は UP / DOWN / ALARM 以外を拒む", "error" in graph.set_status("dc1-leaf-01", "", "broken") and len(state["queries"]) == 1)
state.update(answer={"fold()": [], "coalesce(": []}, queries=[])
r = graph.set_status("zz-ce-09", "", "ALARM")
check("トポロジに無い機器の異常は捨てず、未登録の頂点（role=unknown, registered=false）を coalesce で作って status を書く",
      r == {"device_id": "zz-ce-09", "status": "ALARM", "updated": 0, "unregistered": True}
      and state["queries"][1] == ("g.V('zz-ce-09').fold().coalesce(unfold(),addV('device').property(id,'zz-ce-09').property('hostname','zz-ce-09')"
                                  ".property('site','?').property('role','unknown').property('enabled',false).property('registered',false))")
      and state["queries"][2] == "g.V('zz-ce-09').property(single,'status','ALARM')")
state.update(answer={"outE('link')": [0], "inE('link')": [0], "fold()": [], "coalesce(": []}, queries=[])
r = graph.set_status("dc1-leaf-01", "eth9", "DOWN")
check("トポロジに無いインタフェースの異常は、機器（無ければ）とインタフェースの未登録の頂点を作る",
      r["unregistered"] is True and r["updated"] == 0
      and "addV('interface').property(id,'dc1-leaf-01#eth9').property('device_id','dc1-leaf-01').property('name','eth9').property('registered',false)" in state["queries"][4]
      and state["queries"][5] == "g.V('dc1-leaf-01#eth9').property(single,'status','DOWN')")
state.update(answer={"outE('link')": [0], "inE('link')": [0], "coalesce(": []}, queries=[])
r = graph.set_status("zz-ce-09", "eth1", "UP")
check("UP に戻すだけのときは未登録の頂点を作らない", "unregistered" not in r and not any("addV" in q for q in state["queries"]))
# 上の層の動的な状態（bgp_down / isis_down。graph/status_handler.py が set_layer_status を呼ぶ）
state.update(answer={"coalesce(": [True]}, queries=[])
check("set_layer_status は <機器>#bgp#<相手の IP> の bgp_session の頂点に single で書く",
      graph.set_layer_status("dc1-leaf-01", "bgp", "10.255.0.1", "DOWN") == {"device_id": "dc1-leaf-01", "kind": "bgp", "target": "10.255.0.1", "status": "DOWN", "updated": 1}
      and state["queries"] == ["g.V('dc1-leaf-01#bgp#10.255.0.1').hasLabel('bgp_session').property(single,'status','DOWN').coalesce(values('registered'),constant(true))"])
state.update(answer={"coalesce(": [True]}, queries=[])
check("isis は isis_adjacency の頂点（target = サブインタフェース）", graph.set_layer_status("dc1-leaf-01", "isis", "ethernet-1/1.0", "up")["updated"] == 1
      and state["queries"][0].startswith("g.V('dc1-leaf-01#isis#ethernet-1/1.0').hasLabel('isis_adjacency').property(single,'status','UP')"))
state.update(answer={"coalesce(": []}, queries=[])
r = graph.set_layer_status("dc1-leaf-01", "bgp", "10.255.9.9", "DOWN")
check("トポロジに無いセッションは未登録の頂点（layer と peer_address 付き）を作って status を書く", r["unregistered"] is True and r["updated"] == 0
      and "addV('bgp_session').property(id,'dc1-leaf-01#bgp#10.255.9.9').property('device_id','dc1-leaf-01').property('layer','evpn').property('peer_address','10.255.9.9').property('registered',false)" in state["queries"][1]
      and state["queries"][2] == "g.V('dc1-leaf-01#bgp#10.255.9.9').property(single,'status','DOWN')")
state.update(answer={"coalesce(": []}, queries=[])
check("UP に戻すだけなら未登録の頂点を作らない", "unregistered" not in graph.set_layer_status("dc1-leaf-01", "isis", "x", "UP") and len(state["queries"]) == 1)
check("kind / status が違えば error", "error" in graph.set_layer_status("d", "ospf", "x") and "error" in graph.set_layer_status("d", "bgp", "x", "broken"))
state.update(answer={"coalesce(": [False]}, queries=[])
check("未登録の頂点に書いたときも unregistered", graph.set_status("zz-ce-09", "", "UP").get("unregistered") is True and len(state["queries"]) == 1)
devs[0]["status"] = "DOWN"; links[0]["status"] = "DOWN"
devs.append({"id": "zz-ce-09", "label": "device", "hostname": "zz-ce-09", "site": "?", "role": "unknown", "enabled": False, "registered": False, "status": "ALARM"})
state.update(answer={"g.V().hasLabel('device').elementMap()": devs, "g.V().hasLabel('interface').elementMap()": ifs,
                     "g.E().hasLabel('link').elementMap()": links, "g.V().hasLabel('ip_interface','isis_adjacency','bgp_session','evpn_instance','ethernet_segment').elementMap()": lv,
                     "g.E().hasLabel('over','peer','tunnel','attach','segment').elementMap()": le}, queries=[])
topology.reload(force=True)
check("Neptune の上の層は topology.layers に出て、未登録の頂点は registered False のまま", topology.layers()["count"] == 2
      and next(v for v in topology.layers()["vertices"] if v["id"] == "a-ce-01#bgp#10.255.0.9")["registered"] is False)
check("Neptune の status は機器一覧・隣接・影響範囲に出て、無ければ UP",
      topology.list_devices()["devices"][0]["status"] == "DOWN" and topology.list_devices()["devices"][1]["status"] == "UP"
      and topology.neighbors("b-ce-01")["neighbors"][0]["status"] == "DOWN"
      and topology.blast_radius("b-ce-01")["affected"][0]["status"] == "DOWN")
rows = topology.list_devices()["devices"]
check("未登録の機器は registered=false・role=unknown で一覧の最後に出る", rows[-1]["device_id"] == "zz-ce-09" and rows[-1]["registered"] is False
      and all(x["registered"] for x in rows[:-1]))
check("interfaces は Neptune のインタフェースの一覧とリンクの IF 名を合わせる", topology.interfaces("a-ce-01") == ["eth0", "eth1"])
print(f"通過 {passed} / 失敗 0")
