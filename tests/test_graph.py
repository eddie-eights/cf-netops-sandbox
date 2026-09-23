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
check("未配備なら静的データ", topology.SOURCE == "static" and len(topology.DEVICES) == 10)

# ---- GraphSON の読み替え
check("_un は List / Map / Int を素の値に", graph._un(gs({"a": [1, 2], "b": "s"})) == {"a": [1, 2], "b": "s"})
check("_q は文字列を ' で囲み ' をエスケープ", graph._q("it's") == "'it\\'s'" and graph._q(5) == "5" and graph._q(True) == "true")

# ---- 配備あり（環境変数）
os.environ["NEPTUNE_ENDPOINT"] = "db.example:8182"
graph = load("graph")
check("環境変数で配備あり", graph.configured() and graph._client().kw["endpoint_url"] == "https://db.example:8182")
devs = [{"id": "a-ce-01", "label": "device", "hostname": "a-ce-01", "site": "a", "role": "ce", "asn": 65001, "mgmt_ip": "203.0.113.11", "enabled": True},
        {"id": "b-ce-01", "label": "device", "hostname": "b-ce-01", "site": "b", "role": "ce", "asn": None, "mgmt_ip": "203.0.113.12", "enabled": False}]
ifs = [{"id": "a-ce-01#eth0", "label": "interface", "device_id": "a-ce-01", "name": "eth0", "address": "203.0.113.11"},
       {"id": "a-ce-01#eth1", "label": "interface", "device_id": "a-ce-01", "name": "eth1", "address": "172.16.1.2", "status": "DOWN"},
       {"id": "gone#eth1", "label": "interface", "device_id": "gone", "name": "eth1"}]
links = [{"id": "e1", "label": "link", "OUT": {"id": "a-ce-01"}, "IN": {"id": "b-ce-01"}, "a_if": "eth1", "b_if": "eth1", "kind": "l2", "role": "primary", "bandwidth_mbps": 1000}]
state.update(answer={"g.V().hasLabel('device').elementMap()": devs, "g.V().hasLabel('interface').elementMap()": ifs,
                     "g.E().hasLabel('link').elementMap()": links}, queries=[])
d, l = graph.load_topology()
check("load_topology はインタフェースの頂点を device_id で機器に付け、機器の無いものは捨てる",
      d[0]["interfaces"] == [{"name": "eth0", "address": "203.0.113.11", "status": None, "registered": True},
                             {"name": "eth1", "address": "172.16.1.2", "status": "DOWN", "registered": True}]
      and d[1]["interfaces"] == [] and d[0]["registered"] is True)
check("load_topology は device_id と a/b を組み立てる", d[0]["device_id"] == "a-ce-01" and d[0]["asn"] == 65001 and d[1]["enabled"] is False and l == [{"a_if": "eth1", "b_if": "eth1", "kind": "l2", "role": "primary", "bandwidth_mbps": 1000, "status": None, "a": "a-ce-01", "b": "b-ce-01"}])

topology = load("topology")
check("配備ありなら Neptune から組む", topology.SOURCE == "neptune" and [x["device_id"] for x in topology.DEVICES] == ["a-ce-01", "b-ce-01"])
check("隣接も Neptune の辺から", topology.neighbors("a-ce-01")["neighbors"][0]["device_id"] == "b-ce-01")
check("topology_graph の source は neptune", topology.topology_graph()["source"] == "neptune")

# ---- 読めなければ静的へ
state["fail"] = True
check("Neptune が落ちていれば静的データに戻る", topology.reload(force=True) == "static" and len(topology.DEVICES) == 10)
state["fail"] = False
state.update(answer={"g.V().hasLabel('device').elementMap()": [], "g.V().hasLabel('interface').elementMap()": [], "g.E().hasLabel('link').elementMap()": []})
check("Neptune が空なら静的データを見せて neptune-empty", topology.reload(force=True) == "neptune-empty" and len(topology.DEVICES) == 10)

# ---- 書き込みの Gremlin
state.update(answer={"count()": [1]}, queries=[])
check("add_link は同じ機器を拒む", "error" in graph.add_link("a", "e1", "a", "e2"))
state.update(answer={"outE('link')": [0], "count()": [1]}, queries=[])
r = graph.add_link("b-ce-01", "eth2", "a-ce-01", "eth3", "ebgp", "secondary", 100)
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
check("count は登録済みの機器・IF・回線と未登録の頂点を数える", graph.count() == {"devices": 3, "interfaces": 3, "links": 3, "unregistered": 3}
      and state["queries"][0] == "g.V().hasLabel('device').hasNot('registered').count()" and state["queries"][3] == "g.V().has('registered',false).count()")
# 判定は辞書の順（outE の count は 0 = リンク未登録、機器の count は 1 = 登録済み）
spec = importlib.util.spec_from_file_location("lab_topology", os.path.join(AGENT, "..", "lab", "lab_topology.py"))
lt = importlib.util.module_from_spec(spec); spec.loader.exec_module(lt)
lab_devices, lab_links = lt.load(os.path.join(AGENT, "..", "lab"))
n_if = sum(len(x["interfaces"]) for x in lab_devices)
placeholders = [{"id": "hq-ce-01", "label": "device", "registered": False, "role": "unknown", "status": "ALARM"},
                {"id": "hq-ce-01#eth1", "label": "interface", "registered": False, "device_id": "hq-ce-01", "name": "eth1", "status": "DOWN"},
                {"id": "zz-ce-09", "label": "device", "registered": False, "role": "unknown", "status": "ALARM"}]
state.update(answer={"has('registered',false).elementMap()": placeholders, "outE('link')": [0], "inE('link')": [0], "drop()": [], "addV": [], "addE": [], "count()": [1]}, queries=[])
r = graph.seed(lab_devices, lab_links)
qs = state["queries"]
check("seed は未登録の頂点を読み、登録済みを drop してから、今回登録される未登録の頂点だけ drop する",
      qs[0] == "g.V().has('registered',false).elementMap()" and qs[1] == "g.V().hasLabel('device','interface').hasNot('registered').drop()"
      and qs[2:4] == ["g.V('hq-ce-01').drop()", "g.V('hq-ce-01#eth1').drop()"] and not any("'zz-ce-09'" in q for q in qs))
check(f"seed は lab の 10 台と全インタフェース {n_if} 個を addV、10 本を addE", n_if > 20
      and sum(q.startswith("g.addV('device')") for q in qs) == 10 and sum(q.startswith("g.addV('interface')") for q in qs) == n_if
      and "g.addV('interface').property(id,'hq-ce-01#eth0').property('device_id','hq-ce-01').property('name','eth0').property('address','203.0.113.11')" in qs
      and sum(q.startswith("g.addE") for q in qs) == 10)
check("seed は status を入れず、置き換えた未登録の頂点の UP でない status だけ引き継ぐ",
      [q for q in qs if "'status'" in q] == [
          "g.V('hq-ce-01').property(single,'status','ALARM').coalesce(values('registered'),constant(true))",
          "g.V('hq-ce-01').outE('link').has('a_if','eth1').property('status','DOWN').count()",
          "g.V('hq-ce-01').inE('link').has('b_if','eth1').property('status','DOWN').count()",
          "g.V('hq-ce-01#eth1').property(single,'status','DOWN').coalesce(values('registered'),constant(true))"])
state.update(answer={"has('registered',false).elementMap()": [], "outE('link')": [0], "drop()": [], "addV": [], "addE": [], "count()": [1]}, queries=[])
r = graph.seed(*topology.load_static())
check("静的データ（インタフェースの一覧が無い）でも seed は 10 台と 10 本", sum(q.startswith("g.addV('device')") for q in state["queries"]) == 10
      and not any(q.startswith("g.addV('interface')") for q in state["queries"]) and sum(q.startswith("g.addE") for q in state["queries"]) == 10
      and not any("'status'" in q for q in state["queries"]))

# ---- 動的な状態（graph/status_handler.py が呼ぶ）
state.update(answer={"outE('link')": [1], "inE('link')": [0], "coalesce(": [True]}, queries=[])
r = graph.set_status("hq-ce-01", "eth1", "down")
check("set_status は IF 付きなら a 側の outE と b 側の inE の辺と、インタフェースの頂点（single）に書き、更新数を返す",
      r == {"device_id": "hq-ce-01", "if_name": "eth1", "status": "DOWN", "updated": 2}
      and state["queries"] == ["g.V('hq-ce-01').outE('link').has('a_if','eth1').property('status','DOWN').count()",
                               "g.V('hq-ce-01').inE('link').has('b_if','eth1').property('status','DOWN').count()",
                               "g.V('hq-ce-01#eth1').property(single,'status','DOWN').coalesce(values('registered'),constant(true))"])
state.update(answer={"coalesce(": [True]}, queries=[])
check("set_status は IF 無しなら機器の頂点に single で書く（Neptune の既定の set だと値が積み重なる）",
      graph.set_status("hq-ce-01", "", "ALARM") == {"device_id": "hq-ce-01", "status": "ALARM", "updated": 1}
      and state["queries"] == ["g.V('hq-ce-01').property(single,'status','ALARM').coalesce(values('registered'),constant(true))"])
check("set_status は UP / DOWN / ALARM 以外を拒む", "error" in graph.set_status("hq-ce-01", "", "broken") and len(state["queries"]) == 1)
state.update(answer={"fold()": [], "coalesce(": []}, queries=[])
r = graph.set_status("zz-ce-09", "", "ALARM")
check("トポロジに無い機器の異常は捨てず、未登録の頂点（role=unknown, registered=false）を coalesce で作って status を書く",
      r == {"device_id": "zz-ce-09", "status": "ALARM", "updated": 0, "unregistered": True}
      and state["queries"][1] == ("g.V('zz-ce-09').fold().coalesce(unfold(),addV('device').property(id,'zz-ce-09').property('hostname','zz-ce-09')"
                                  ".property('site','?').property('role','unknown').property('enabled',false).property('registered',false))")
      and state["queries"][2] == "g.V('zz-ce-09').property(single,'status','ALARM')")
state.update(answer={"outE('link')": [0], "inE('link')": [0], "fold()": [], "coalesce(": []}, queries=[])
r = graph.set_status("hq-ce-01", "eth9", "DOWN")
check("トポロジに無いインタフェースの異常は、機器（無ければ）とインタフェースの未登録の頂点を作る",
      r["unregistered"] is True and r["updated"] == 0
      and "addV('interface').property(id,'hq-ce-01#eth9').property('device_id','hq-ce-01').property('name','eth9').property('registered',false)" in state["queries"][4]
      and state["queries"][5] == "g.V('hq-ce-01#eth9').property(single,'status','DOWN')")
state.update(answer={"outE('link')": [0], "inE('link')": [0], "coalesce(": []}, queries=[])
r = graph.set_status("zz-ce-09", "eth1", "UP")
check("UP に戻すだけのときは未登録の頂点を作らない", "unregistered" not in r and not any("addV" in q for q in state["queries"]))
state.update(answer={"coalesce(": [False]}, queries=[])
check("未登録の頂点に書いたときも unregistered", graph.set_status("zz-ce-09", "", "UP").get("unregistered") is True and len(state["queries"]) == 1)
devs[0]["status"] = "DOWN"; links[0]["status"] = "DOWN"
devs.append({"id": "zz-ce-09", "label": "device", "hostname": "zz-ce-09", "site": "?", "role": "unknown", "enabled": False, "registered": False, "status": "ALARM"})
state.update(answer={"g.V().hasLabel('device').elementMap()": devs, "g.V().hasLabel('interface').elementMap()": ifs,
                     "g.E().hasLabel('link').elementMap()": links}, queries=[])
topology.reload(force=True)
check("Neptune の status は機器一覧・隣接・影響範囲に出て、無ければ UP",
      topology.list_devices()["devices"][0]["status"] == "DOWN" and topology.list_devices()["devices"][1]["status"] == "UP"
      and topology.neighbors("b-ce-01")["neighbors"][0]["status"] == "DOWN"
      and topology.blast_radius("b-ce-01")["affected"][0]["status"] == "DOWN")
rows = topology.list_devices()["devices"]
check("未登録の機器は registered=false・role=unknown で一覧の最後に出る", rows[-1]["device_id"] == "zz-ce-09" and rows[-1]["registered"] is False
      and all(x["registered"] for x in rows[:-1]))
check("interfaces は Neptune のインタフェースの一覧とリンクの IF 名を合わせる", topology.interfaces("a-ce-01") == ["eth0", "eth1"])
print(f"通過 {passed} / 失敗 0")
