"""stream/detector.py の模擬テスト。boto3 を差し替えて、stream.yaml に埋めた ZipFile と一致することも確かめる。
実行は python3 tests/test_stream.py（依存は無い）。"""
import base64, importlib.util, json, os, re, sys, types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "stream", "detector.py")

passed = 0
def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)

# ---- stream.yaml の ZipFile（2 つ目 = DetectorFunction）と detector.py が同じか
with open(os.path.join(ROOT, "stream.yaml"), encoding="utf-8") as f:
    y = f.read()
with open(SRC, encoding="utf-8") as f:
    src = f.read()
starts = [m.end() for m in re.finditer(r"        ZipFile: \|\n", y)]
check("stream.yaml に ZipFile は 2 つ（bootstrap / detector）", len(starts) == 2)
body = []
for line in y[starts[1]:].split("\n"):
    if line.strip() and not line.startswith("          "):
        break
    body.append(line[10:] if line.startswith("          ") else "")
embedded = "\n".join(body).rstrip("\n") + "\n"
check("ZipFile と stream/detector.py が一致", embedded == src)
check("ZipFile は 4096 文字以内", len(src) <= 4096)

# ---- boto3 の差し替え
class CondFail(Exception):
    pass

class FakeTable:
    def __init__(self):
        self.items = {}
        self.meta = types.SimpleNamespace(client=types.SimpleNamespace(exceptions=types.SimpleNamespace(ConditionalCheckFailedException=CondFail)))
    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, ConditionExpression=None):
        k = Key["anomaly_id"]
        it = self.items.get(k)
        if ConditionExpression:
            if not it or it.get("status") != ExpressionAttributeValues[":o"]:
                raise CondFail()
        it = dict(it or {})
        for part in UpdateExpression[len("SET "):].split(", "):
            lhs, rhs = part.split("=", 1)
            lhs = ExpressionAttributeNames.get(lhs, lhs)
            if rhs.startswith("if_not_exists("):
                attr, default = rhs[len("if_not_exists("):-1].split(",")
                it[lhs] = it.get(attr, ExpressionAttributeValues[default])
            else:
                it[lhs] = ExpressionAttributeValues[rhs]
        self.items[k] = it

table = FakeTable()
boto3 = types.ModuleType("boto3")
boto3.resource = lambda name: types.SimpleNamespace(Table=lambda n: table)
sys.modules["boto3"] = boto3
os.environ.update({"TABLE": "t", "DEVICE_MAP": "203.0.113.11=hq-ce-01,203.0.113.12=dc-ce-01"})
spec = importlib.util.spec_from_file_location("detector", SRC)
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)

def rec(m):
    return {"value": base64.b64encode(json.dumps(m).encode()).decode()}

def ev(*ms):
    return {"records": {"metrics-0": [rec(m) for m in ms]}}

def poll(ifn, status, sysname="hq-ce-01", source="203.0.113.11"):
    return {"name": "interface", "tags": {"sysName": sysname, "source": source, "ifDescr": ifn}, "fields": {"ifOperStatus": status, "ifInOctets": 1}}

# ---- 機器名
check("sysName が優先", d.device({"tags": {"sysName": "x", "source": "203.0.113.11"}}) == "x")
check("sysName が無ければ DEVICE_MAP", d.device({"tags": {"source": "203.0.113.12"}}) == "dc-ce-01")
check("agent_host（旧タグ）でも引ける", d.device({"tags": {"agent_host": "203.0.113.11"}}) == "hq-ce-01")
check("地図に無ければ IP のまま", d.device({"tags": {"source": "203.0.113.99"}}) == "203.0.113.99")

# ---- ポーリング
check("正常なポーリングは何も書かない", d.handler(ev(poll("eth1", 1)), None) == {"processed": 0} and table.items == {})
check("lo は無視", d.handler(ev(poll("lo", 2)), None) == {"processed": 0})
r = d.handler(ev(poll("eth1", 2)), None)
it = table.items["hq-ce-01#link_down#eth1"]
check("down で open を書く", r["processed"] == 1 and it["status"] == "open" and it["source"] == "poll" and it["kind"] == "link_down" and it["target"] == "eth1")
first = it["first_seen"]
d.handler(ev(poll("eth1", 2)), None)
check("続く down は first_seen を変えない", table.items["hq-ce-01#link_down#eth1"]["first_seen"] == first)
r = d.handler(ev(poll("eth1", 1)), None)
it = table.items["hq-ce-01#link_down#eth1"]
check("up で resolved にする", r["processed"] == 1 and it["status"] == "resolved" and "resolved_at" in it)
check("解消済みへの up は数えない", d.handler(ev(poll("eth1", 1)), None)["processed"] == 0)
check("ifDescr が無ければ ifIndex", d.events({"name": "interface", "tags": {"sysName": "a", "ifIndex": "3"}, "fields": {"ifOperStatus": 2}})[0][2] == "3")

# ---- trap
def trap(oid, fields, source="203.0.113.13"):
    return {"name": "snmp_trap", "tags": {"oid": oid, "source": source, "version": "2c"}, "fields": fields}

r = d.handler(ev(trap(d.LINK_DOWN, {".1.3.6.1.2.1.2.2.1.1.2": 2, ".1.3.6.1.2.1.2.2.1.8.2": 2, ".1.3.6.1.2.1.2.2.1.2.2": "eth1"})), None)
it = table.items["br1-ce-01#link_down#eth1"] if "br1-ce-01#link_down#eth1" in table.items else None
check("linkDown trap は MIB 無しの数値 OID から ifDescr を取り、DEVICE_MAP に無い機器は IP", it is None and table.items["203.0.113.13#link_down#eth1"]["source"] == "trap")
d.handler(ev(trap(d.LINK_DOWN, {"ifIndex": 4, "ifDescr": "eth2"}, source="203.0.113.11")), None)
check("MIB ありの名前でも引ける", table.items["hq-ce-01#link_down#eth2"]["status"] == "open")
d.handler(ev(trap(d.LINK_UP, {"ifDescr": "eth2"}, source="203.0.113.11")), None)
check("linkUp trap で resolved", table.items["hq-ce-01#link_down#eth2"]["status"] == "resolved")
d.handler(ev(trap(".1.3.6.1.6.3.1.1.5.1", {}, source="203.0.113.11")), None)
check("その他の trap は kind=trap で OID を対象に", table.items["hq-ce-01#trap#.1.3.6.1.6.3.1.1.5.1"]["status"] == "open")

# ---- 壊れたレコード
check("JSON でないレコードは飛ばす", d.handler({"records": {"x": [{"value": base64.b64encode(b"nope").decode()}, {"novalue": 1}]}}, None) == {"processed": 0})
check("dict でない JSON も飛ばす", d.handler(ev([1, 2]), None) == {"processed": 0})
check("複数トピックを回す", d.handler({"records": {"a": [rec(poll("eth3", 2))], "b": [rec(poll("eth3", 2, "dc-ce-01"))]}}, None)["processed"] == 2)
print(f"通過 {passed} / 失敗 0")
