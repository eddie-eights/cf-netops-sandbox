"""異常検知（spark/snmp_sinks.py の detect）の模擬テスト。boto3 のクライアントを差し替えて DynamoDB と EventBridge への書き込みを確かめ、
terraform/stream から detector Lambda が消えて anomalies テーブルだけが残っていることも確かめる。
実行は python3 tests/test_stream.py（pyspark も boto3 も要らない。snmp_sinks.py は pyspark を関数の中で import する）。"""
import importlib.util, json, os, re, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "spark", "snmp_sinks.py")

passed = 0
def check(name, cond):
    global passed
    assert cond, name
    passed += 1
    print("ok", name)

# ---- terraform/stream: detector Lambda は analytics の Spark に寄せた
TF_DIR = os.path.join(ROOT, "terraform", "stream")
tf = ""
for name in sorted(os.listdir(TF_DIR)):
    if name.endswith(".tf"):
        with open(os.path.join(TF_DIR, name), encoding="utf-8") as f:
            tf += f.read() + "\n"
check("stream/detector.py は無い（検知は spark/snmp_sinks.py）", not os.path.exists(os.path.join(ROOT, "stream", "detector.py")))
check("terraform/stream に detector の Lambda が無い", '"detector"' not in tf and "stream/detector.py" not in tf and "archive_file" not in tf)
check("terraform/stream に lambda のエンドポイントが無い", '.lambda"' not in tf)
check("terraform/stream は sts のエンドポイントを create_sts_endpoint で切れる", 'variable "create_sts_endpoint"' in tf and 'toset(["sts"])' in tf)
check("terraform/stream は anomalies テーブルを持つ", re.search(r'resource "aws_dynamodb_table" "anomalies"', tf) is not None)
check("output に anomaly_table_name と anomaly_table_arn がある（analytics が読む）",
      'output "anomaly_table_name"' in tf and 'output "anomaly_table_arn"' in tf)
check("detector_logs の output は無い", "detector_logs" not in tf)

# ---- spark/snmp_sinks.py を pyspark 無しで読む
spec = importlib.util.spec_from_file_location("snmp_sinks", SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
with open(SRC, encoding="utf-8") as f:
    src = f.read()
check("argparse に --anomaly-table / --device-map / --event-bus がある",
      all(f'"--{a}"' in src for a in ("anomaly-table", "device-map", "event-bus")))
check("build は --anomaly-table があるときだけ detect のクエリを足す",
      re.search(r'if args\.anomaly_table:[\s\S]*?http_query\(rows, "detect"', src) is not None)
check("Source は netops.spark、DetailType は AnomalyOpened / AnomalyResolved", mod.EVENT_SOURCE == "netops.spark" and mod.EVENT_DETAIL_TYPE == "AnomalyOpened" and mod.EVENT_RESOLVED_TYPE == "AnomalyResolved")
check("parse_device_map は = の無い要素を捨てる",
      mod.parse_device_map("203.0.113.11=hq-ce-01,garbage,203.0.113.12=dc-ce-01") == {"203.0.113.11": "hq-ce-01", "203.0.113.12": "dc-ce-01"}
      and mod.parse_device_map("") == {})


# ---- boto3 の低レベルクライアントの模擬
class ConditionalCheckFailedException(Exception):
    pass


class FakeDynamo:
    """update_item だけ。SET a=:x, b=if_not_exists(b,:y) と ConditionExpression "#s = :o" を読む"""
    class exceptions:
        ConditionalCheckFailedException = ConditionalCheckFailedException

    def __init__(self):
        self.items = {}
        self.calls = []

    def update_item(self, **kw):
        self.calls.append(kw)
        key = kw["Key"]["anomaly_id"]["S"]
        names, values = kw.get("ExpressionAttributeNames", {}), kw["ExpressionAttributeValues"]
        old = self.items.get(key)
        cond = kw.get("ConditionExpression")
        if cond:
            m = re.fullmatch(r"(\S+) = (:\w+)", cond)
            attr = names.get(m.group(1), m.group(1))
            if old is None or old.get(attr) != values[m.group(2)]:
                raise ConditionalCheckFailedException("condition")
        item = dict(old or {})
        for lhs, rhs in re.findall(r"([#\w]+)=(if_not_exists\([^)]*\)|:\w+)", kw["UpdateExpression"]):
            attr = names.get(lhs, lhs)
            m = re.fullmatch(r"if_not_exists\((\w+),(:\w+)\)", rhs)
            if m:
                if attr not in item:
                    item[attr] = values[m.group(2)]
            else:
                item[attr] = values[rhs]
        self.items[key] = item
        if kw.get("ReturnValues") == "ALL_OLD" and old is not None:
            return {"Attributes": old}
        return {}

    def plain(self, key):
        return {k: (int(v["N"]) if "N" in v else v["S"]) for k, v in self.items[key].items()}


class FakeEvents:
    def __init__(self):
        self.calls = []

    def put_events(self, Entries):
        assert len(Entries) <= 10, "PutEvents は 1 回 10 件まで"
        self.calls.append(Entries)
        return {"FailedEntryCount": 0}

    def details(self):
        return [json.loads(e["Detail"]) for call in self.calls for e in call]


def make():
    ddb, ev = FakeDynamo(), FakeEvents()
    send = mod.make_detect_sender("t", mod.parse_device_map("203.0.113.11=hq-ce-01,203.0.113.12=dc-ce-01"), "ap-northeast-1", "default",
                                  dynamodb=ddb, events_client=ev)
    return send, ddb, ev


def iface(host, ifn, status, sysname=None, ifindex=None):
    tags = {"agent_host": host}
    if ifn is not None:
        tags["ifDescr"] = ifn
    if ifindex is not None:
        tags["ifIndex"] = ifindex
    if sysname:
        tags["sysName"] = sysname
    return {"measurement": "interface", "tags": tags, "fields": {"ifOperStatus": status}}


def trap(host, oid, fields):
    return {"measurement": "snmp_trap", "tags": {"agent_host": host, "oid": oid}, "fields": fields}


# ---- 機器名
check("機器名は sysName > device map > IP > ?",
      mod.device({"tags": {"agent_host": "203.0.113.11", "sysName": "r1"}}, {"203.0.113.11": "hq-ce-01"}) == "r1"
      and mod.device({"tags": {"agent_host": "203.0.113.11"}}, {"203.0.113.11": "hq-ce-01"}) == "hq-ce-01"
      and mod.device({"tags": {"source": "203.0.113.99"}}, {}) == "203.0.113.99"
      and mod.device({"tags": {}}, {}) == "?")

# ---- ポーリング
send, ddb, ev = make()
check("正常なポーリング（up）は何も書かない", send([iface("203.0.113.11", "eth1", 1)]) == [] and ddb.items == {} and ev.calls == [])
check("lo は見ない", send([iface("203.0.113.11", "lo", 2)]) == [] and ddb.items == {})
check("dict でない行や関係ない measurement は捨てる",
      send([None, "garbage", {"measurement": "cpu", "tags": {}, "fields": {"usage": 1}}, {"measurement": "interface", "tags": {}, "fields": {}}]) == []
      and ddb.items == {})

opened = send([iface("203.0.113.11", "eth1", 2)])
key = "hq-ce-01#link_down#eth1"
row = ddb.plain(key)
check("down → open（device_id / kind / target / source=poll / detail / first_seen=last_seen）",
      row["status"] == "open" and row["device_id"] == "hq-ce-01" and row["kind"] == "link_down" and row["target"] == "eth1"
      and row["source"] == "poll" and row["detail"] == "eth1 is down (poll)" and row["first_seen"] == row["last_seen"])
check("新しく open になったものだけ返し、AnomalyOpened を 1 件出す",
      [o["anomaly_id"] for o in opened] == [key] and len(ev.calls) == 1 and len(ev.calls[0]) == 1)
entry = ev.calls[0][0]
detail = json.loads(entry["Detail"])
check("put_events の Source / DetailType / EventBusName", entry["Source"] == "netops.spark" and entry["DetailType"] == "AnomalyOpened" and entry["EventBusName"] == "default")
check("Detail に anomaly_id / device_id / kind / target / first_seen / detail / source",
      detail == {"anomaly_id": key, "device_id": "hq-ce-01", "kind": "link_down", "target": "eth1", "first_seen": row["first_seen"],
                 "detail": "eth1 is down (poll)", "source": "poll"})

first = row["first_seen"]
ddb.items[key]["first_seen"] = {"N": str(first - 100)}   # 前から開いていたことにする
check("開いたままの down は first_seen を残し、イベントは出さない",
      send([iface("203.0.113.11", "eth1", 2)]) == [] and ddb.plain(key)["first_seen"] == first - 100 and len(ev.calls) == 1)
check("up → resolved（resolved_at が付く）",
      send([iface("203.0.113.11", "eth1", 1)]) == [] and ddb.plain(key)["status"] == "resolved" and "resolved_at" in ddb.plain(key))
entry = ev.calls[-1][0]
check("open → resolved で AnomalyResolved を 1 件出す（Detail に anomaly_id / device_id / kind / target / resolved_at / source）",
      len(ev.calls) == 2 and len(ev.calls[1]) == 1 and entry["DetailType"] == "AnomalyResolved" and entry["Source"] == "netops.spark"
      and json.loads(entry["Detail"]) == {"anomaly_id": key, "device_id": "hq-ce-01", "kind": "link_down", "target": "eth1",
                                          "resolved_at": ddb.plain(key)["resolved_at"], "source": "poll"})
check("resolved のあとの up は何もしない（ConditionExpression。AnomalyResolved も出さない）",
      send([iface("203.0.113.11", "eth1", 1)]) == [] and ddb.plain(key)["status"] == "resolved" and len(ev.calls) == 2)
reopened = send([iface("203.0.113.11", "eth1", 2)])
check("resolved → 再 open はイベントをもう一度出し、first_seen は前のまま",
      len(reopened) == 1 and reopened[0]["first_seen"] == first - 100 and ddb.plain(key)["status"] == "open"
      and ddb.plain(key)["first_seen"] == first - 100 and len(ev.calls) == 3 and ev.calls[-1][0]["DetailType"] == "AnomalyOpened")

send, ddb, ev = make()
check("ifDescr が無ければ ifIndex", send([iface("203.0.113.12", None, 2, ifindex="3")]) and "dc-ce-01#link_down#3" in ddb.items)
send, ddb, ev = make()
check("sysName があれば device map より優先", send([iface("203.0.113.12", "eth0", "2", sysname="r2")]) and "r2#link_down#eth0" in ddb.items)
send, ddb, ev = make()
check("同じキーが 1 バッチに何度も出たら最後の状態だけ書く（down → up なら何も残らない）",
      send([iface("203.0.113.11", "eth1", 2), iface("203.0.113.11", "eth1", 1)]) == [] and ddb.items == {} and ev.calls == [])
send, ddb, ev = make()
check("同じキーが 1 バッチに何度も出ても DynamoDB へは 1 回",
      len(send([iface("203.0.113.11", "eth1", 2), iface("203.0.113.11", "eth1", 2)])) == 1 and len(ddb.calls) == 1)

# ---- trap
send, ddb, ev = make()
opened = send([trap("203.0.113.11", mod.LINK_DOWN, {".1.3.6.1.2.1.2.2.1.2.3": "eth3", ".1.3.6.1.2.1.2.2.1.1.3": 3})])
key = "hq-ce-01#link_down#eth3"
check("linkDown trap（MIB 無しの数値 OID）は ifDescr の varbind から target を取り source=trap",
      [o["anomaly_id"] for o in opened] == [key] and ddb.plain(key)["source"] == "trap" and ddb.plain(key)["detail"] == "eth3 is down (trap)")
check("linkUp trap で resolved", send([trap("203.0.113.11", mod.LINK_UP, {".1.3.6.1.2.1.2.2.1.2.3": "eth3"})]) == [] and ddb.plain(key)["status"] == "resolved")
send, ddb, ev = make()
send([trap("203.0.113.11", mod.LINK_DOWN, {"ifDescr": "eth4", "ifIndex": 4})])
check("MIB がある varbind 名（ifDescr）でも取れる", "hq-ce-01#link_down#eth4" in ddb.items)
send, ddb, ev = make()
send([trap("203.0.113.11", mod.LINK_DOWN, {"ifIndex.5": 5})])
check("ifDescr が無い trap は ifIndex", "hq-ce-01#link_down#5" in ddb.items)
send, ddb, ev = make()
opened = send([trap("203.0.113.11", ".1.3.6.1.6.3.1.1.5.5", {})])
key = "hq-ce-01#trap#.1.3.6.1.6.3.1.1.5.5"
check("linkDown / linkUp 以外の trap は kind=trap で open", key in ddb.items and ddb.plain(key)["kind"] == "trap" and ddb.plain(key)["detail"] == "trap .1.3.6.1.6.3.1.1.5.5"
      and opened[0]["kind"] == "trap")

# ---- PutEvents の 10 件制限
send, ddb, ev = make()
opened = send([iface("203.0.113.11", f"eth{i}", 2) for i in range(23)])
check("新しい異常が 10 件を超えたら put_events を分ける", len(opened) == 23 and [len(c) for c in ev.calls] == [10, 10, 3])

print(f"通過 {passed} / 失敗 0")
