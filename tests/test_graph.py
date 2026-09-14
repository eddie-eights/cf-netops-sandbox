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
sys.modules.update({"boto3": boto3, "botocore": botocore, "botocore.exceptions": exc})

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
links = [{"id": "e1", "label": "link", "OUT": {"id": "a-ce-01"}, "IN": {"id": "b-ce-01"}, "a_if": "eth1", "b_if": "eth1", "kind": "l2", "role": "primary", "bandwidth_mbps": 1000}]
state.update(answer={"g.V().hasLabel('device').elementMap()": devs, "g.E().hasLabel('link').elementMap()": links}, queries=[])
d, l = graph.load_topology()
check("load_topology は device_id と a/b を組み立てる", d[0]["device_id"] == "a-ce-01" and d[0]["asn"] == 65001 and d[1]["enabled"] is False and l == [{"a_if": "eth1", "b_if": "eth1", "kind": "l2", "role": "primary", "bandwidth_mbps": 1000, "a": "a-ce-01", "b": "b-ce-01"}])

topology = load("topology")
check("配備ありなら Neptune から組む", topology.SOURCE == "neptune" and [x["device_id"] for x in topology.DEVICES] == ["a-ce-01", "b-ce-01"])
check("隣接も Neptune の辺から", topology.neighbors("a-ce-01")["neighbors"][0]["device_id"] == "b-ce-01")
check("topology_graph の source は neptune", topology.topology_graph()["source"] == "neptune")

# ---- 読めなければ静的へ
state["fail"] = True
check("Neptune が落ちていれば静的データに戻る", topology.reload(force=True) == "static" and len(topology.DEVICES) == 10)
state["fail"] = False
state.update(answer={"g.V().hasLabel('device').elementMap()": [], "g.E().hasLabel('link').elementMap()": []})
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
state.update(answer={"count()": [0]}, queries=[])
check("add_device は property を並べる", graph.add_device("c-ce-01", "c", "ce", "203.0.113.15", 65003) == {"added": "c-ce-01"} and ".property('asn',65003)" in state["queries"][-1] and ".property('enabled',false)" in state["queries"][-1])
check("remove_device は無ければ error", "error" in graph.remove_device("zzz"))
# 判定は辞書の順（outE の count は 0 = リンク未登録、機器の count は 1 = 登録済み）
state.update(answer={"outE('link')": [0], "drop()": [], "addV": [], "addE": [], "count()": [1]}, queries=[])
r = graph.seed(*topology.load_static())
check("seed は drop してから 10 台と 10 本を addV / addE", state["queries"][0] == "g.V().hasLabel('device').drop()" and sum(q.startswith("g.addV") for q in state["queries"]) == 10 and sum(q.startswith("g.addE") for q in state["queries"]) == 10)
print(f"通過 {passed} / 失敗 0")
