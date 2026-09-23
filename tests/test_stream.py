"""異常検知（spark/snmp_sinks.py の detect）の模擬テスト。boto3 のクライアントを差し替えて DynamoDB と EventBridge への書き込みを確かめ、
terraform/pipeline/stream から detector Lambda が消えて anomalies テーブルだけが残っていることも確かめる。
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

# ---- terraform/pipeline/stream: detector Lambda は analytics の Spark に寄せた
TF_DIR = os.path.join(ROOT, "terraform", "pipeline", "stream")
tf = ""
for name in sorted(os.listdir(TF_DIR)):
    if name.endswith(".tf"):
        with open(os.path.join(TF_DIR, name), encoding="utf-8") as f:
            tf += f.read() + "\n"
check("stream/detector.py は無い（検知は spark/snmp_sinks.py）", not os.path.exists(os.path.join(ROOT, "stream", "detector.py")))
check("terraform/pipeline/stream に detector の Lambda が無い", '"detector"' not in tf and "stream/detector.py" not in tf and "archive_file" not in tf)
check("terraform/pipeline/stream に lambda のエンドポイントが無い", '.lambda"' not in tf)
check("terraform/pipeline/stream は sts のエンドポイントを create_sts_endpoint で切れる", 'variable "create_sts_endpoint"' in tf and 'toset(["sts"])' in tf)
check("terraform/pipeline/stream は anomalies テーブルを持つ", re.search(r'resource "aws_dynamodb_table" "anomalies"', tf) is not None)
check("output に anomaly_table_name と anomaly_table_arn がある（analytics が読む）",
      'output "anomaly_table_name"' in tf and 'output "anomaly_table_arn"' in tf)
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
check("argparse に --anomaly-table / --device-map / --event-bus / --event-source がある",
      all(f'"--{a}"' in src for a in ("anomaly-table", "device-map", "event-bus", "event-source")))
check("build は --anomaly-table があるときだけ detect のクエリを足す",
      re.search(r'if args\.anomaly_table:[\s\S]*?http_query\(rows, "detect"', src) is not None)
check("Source の既定は netops.spark（terraform は <接頭辞>.spark を渡す）、DetailType は AnomalyOpened / AnomalyResolved",
      mod.EVENT_SOURCE == "netops.spark" and mod.EVENT_DETAIL_TYPE == "AnomalyOpened" and mod.EVENT_RESOLVED_TYPE == "AnomalyResolved"
      and mod.parse_args(["--bootstrap", "b", "--checkpoint", "c", "--sinks", "iceberg", "--iceberg-table", "cat.ns.t"]).event_source == "netops.spark")
check("parse_device_map は小文字にそろえ、空白と空の要素を落とす",
      mod.parse_device_map(" HQ-CE-01.lab.example = hq-ce-01 ,=x,y=") == {"hq-ce-01.lab.example": "hq-ce-01"})
check("device は sysName を小文字・FQDN の先頭で引き、無ければ送り元 IP を device map で引く（どれも無ければ IP / ?）",
      mod.device({"tags": {"sysName": "HQ-CE-01.lab.example"}}, {}) == "hq-ce-01"
      and mod.device({"tags": {"sysName": "core-rtr"}}, {"core-rtr": "hq-ce-01"}) == "hq-ce-01"
      and mod.device({"tags": {"sysName": "hq-ce-01.lab.example"}}, {"hq-ce-01.lab.example": "x"}) == "x"
      and mod.device({"tags": {"sysName": "203.0.113.11"}}, {"203.0.113.11": "hq-ce-01"}) == "hq-ce-01"
      and mod.device({"tags": {"agent_host": "172.16.1.2"}}, {"172.16.1.2": "hq-ce-01"}) == "hq-ce-01"
      and mod.device({"tags": {"source": "198.51.100.9"}}, {}) == "198.51.100.9" and mod.device({"tags": {}}, {}) == "?")
check("parse_device_map は = の無い要素を捨てる",
      mod.parse_device_map("203.0.113.11=hq-ce-01,garbage,203.0.113.12=dc-ce-01") == {"203.0.113.11": "hq-ce-01", "203.0.113.12": "dc-ce-01"}
      and mod.parse_device_map("") == {})


# ---- boto3 の低レベルクライアントの模擬
class ConditionalCheckFailedException(Exception):
    pass


class FakeDynamo:
    """update_item と query（GSI の status-last_seen-index）だけ。条件は A = :x / A <> :x / A < :x / attribute_not_exists(A) を AND / OR でつないだものを読む。
    UpdateExpression は SET a=:x, b=if_not_exists(b,:y) と REMOVE。ReturnValues（ALL_NEW / ALL_OLD）と
    ReturnValuesOnConditionCheckFailure（例外の response["Item"]）も本物と同じ形で返す"""
    class exceptions:
        ConditionalCheckFailedException = ConditionalCheckFailedException

    def __init__(self, page=100):
        self.items = {}
        self.calls = []     # update_item の引数
        self.queries = []   # query の引数
        self.page = page    # query の 1 ページの件数（LastEvaluatedKey を試す）

    @staticmethod
    def _v(t):
        if t is None:
            return None
        if "N" in t:
            return int(t["N"])
        return t.get("S", t.get("BOOL"))

    def _cond(self, expr, item, names, values):
        def term(x):
            x = x.strip()
            m = re.fullmatch(r"attribute_not_exists\((\S+)\)", x)
            if m:
                return item is None or names.get(m.group(1), m.group(1)) not in item
            m = re.fullmatch(r"(\S+) (=|<>|<) (:\w+)", x)
            a = self._v((item or {}).get(names.get(m.group(1), m.group(1))))
            b = self._v(values[m.group(3)])
            if m.group(2) == "=":
                return a is not None and a == b
            if m.group(2) == "<>":
                return a != b
            return a is not None and a < b
        return any(all(term(t) for t in alt.split(" AND ")) for alt in expr.split(" OR "))

    def update_item(self, **kw):
        self.calls.append(kw)
        key = kw["Key"]["anomaly_id"]["S"]
        names, values = kw.get("ExpressionAttributeNames", {}), kw["ExpressionAttributeValues"]
        old = self.items.get(key)
        cond = kw.get("ConditionExpression")
        if cond and not self._cond(cond, old, names, values):
            e = ConditionalCheckFailedException("condition")
            e.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            if kw.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD" and old is not None:
                e.response["Item"] = dict(old)
            raise e
        item = dict(old or {"anomaly_id": {"S": key}})
        expr = kw["UpdateExpression"]
        if " REMOVE " in expr:
            expr, removed = expr.split(" REMOVE ", 1)
            for attr in removed.split(","):
                item.pop(names.get(attr.strip(), attr.strip()), None)
        for lhs, rhs in re.findall(r"([#\w]+)=(if_not_exists\([^)]*\)|:\w+)", expr):
            attr = names.get(lhs, lhs)
            m = re.fullmatch(r"if_not_exists\((\w+),(:\w+)\)", rhs)
            if m:
                if attr not in item:
                    item[attr] = values[m.group(2)]
            else:
                item[attr] = values[rhs]
        self.items[key] = item
        if kw.get("ReturnValues") == "ALL_NEW":
            return {"Attributes": dict(item)}
        if kw.get("ReturnValues") == "ALL_OLD" and old is not None:
            return {"Attributes": old}
        return {}

    def query(self, **kw):
        self.queries.append(kw)
        assert kw["IndexName"] == "status-last_seen-index"
        names, values = kw.get("ExpressionAttributeNames", {}), kw["ExpressionAttributeValues"]
        # ページの順は anomaly_id の順にしておく（前のページの行が閉じて外れても、続きの位置が決まる）
        hits = sorted(k for k, it in self.items.items()
                      if self._cond(kw["KeyConditionExpression"], it, names, values)
                      and (not kw.get("FilterExpression") or self._cond(kw["FilterExpression"], it, names, values)))
        if kw.get("ExclusiveStartKey"):
            hits = [k for k in hits if k > kw["ExclusiveStartKey"]["anomaly_id"]["S"]]
        page = hits[:self.page]
        out = {"Items": [dict(self.items[k]) for k in page]}
        if len(hits) > self.page:
            out["LastEvaluatedKey"] = {"anomaly_id": {"S": page[-1]}}
        return out

    def plain(self, key):
        return {k: self._v(v) for k, v in self.items[key].items()}


class FakeEvents:
    """fail に 1 回ごとの振る舞いを積む: "all"（全部 FailedEntry）/ "first"（先頭の 1 件だけ）/ "raise"（例外）。空なら全部通す"""
    def __init__(self):
        self.calls = []
        self.fail = []

    def put_events(self, Entries):
        assert len(Entries) <= 10, "PutEvents は 1 回 10 件まで"
        self.calls.append(Entries)
        mode = self.fail.pop(0) if self.fail else ""
        if mode == "raise":
            raise OSError("events に届かない")
        if mode == "all":
            return {"FailedEntryCount": len(Entries), "Entries": [{"ErrorCode": "InternalFailure"} for _ in Entries]}
        if mode == "first":
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "ThrottlingException"}] + [{"EventId": "e"} for _ in Entries[1:]]}
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "e"} for _ in Entries]}

    def details(self):
        return [json.loads(e["Detail"]) for call in self.calls for e in call]

    def types(self):
        return [e["DetailType"] for call in self.calls for e in call]


class Clock:
    def __init__(self, t=1700000000):
        self.t = t
    def __call__(self):
        return self.t


def make(page=100, clock=None):
    ddb, ev = FakeDynamo(page), FakeEvents()
    sleeps.clear()
    send = mod.make_detect_sender("t", mod.parse_device_map("203.0.113.11=hq-ce-01,203.0.113.12=dc-ce-01"), "ap-northeast-1", "default",
                                  "demo-poc.spark", dynamodb=ddb, events_client=ev, clock=clock or Clock(), sleep=sleeps.append)
    return send, ddb, ev


sleeps = []


def iface(host, ifn, status, sysname=None, ifindex=None, ts=None):
    tags = {"agent_host": host}
    if ifn is not None:
        tags["ifDescr"] = ifn
    if ifindex is not None:
        tags["ifIndex"] = ifindex
    if sysname:
        tags["sysName"] = sysname
    return {"measurement": "interface", "tags": tags, "fields": {"ifOperStatus": status}, "ts": ts}


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
check("put_events の Source（--event-source がそのまま入る）/ DetailType / EventBusName", entry["Source"] == "demo-poc.spark" and entry["DetailType"] == "AnomalyOpened" and entry["EventBusName"] == "default")
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
      len(ev.calls) == 2 and len(ev.calls[1]) == 1 and entry["DetailType"] == "AnomalyResolved" and entry["Source"] == "demo-poc.spark"
      and json.loads(entry["Detail"]) == {"anomaly_id": key, "device_id": "hq-ce-01", "kind": "link_down", "target": "eth1",
                                          "resolved_at": ddb.plain(key)["resolved_at"], "source": "poll"})
check("resolved のあとの up は何もしない（ConditionExpression。AnomalyResolved も出さない）",
      send([iface("203.0.113.11", "eth1", 1)]) == [] and ddb.plain(key)["status"] == "resolved" and len(ev.calls) == 2)
reopened = send([iface("203.0.113.11", "eth1", 2)])
check("resolved → 再 open はイベントをもう一度出し、first_seen を今にして resolved_at を消す（worker が起こし直せるように。2026-09-18）",
      len(reopened) == 1 and reopened[0]["first_seen"] >= first and ddb.plain(key)["status"] == "open"
      and ddb.plain(key)["first_seen"] == reopened[0]["first_seen"] and "resolved_at" not in ddb.plain(key)
      and len(ev.calls) == 3 and ev.calls[-1][0]["DetailType"] == "AnomalyOpened")

send, ddb, ev = make()
check("ifDescr が無ければ ifIndex", send([iface("203.0.113.12", None, 2, ifindex="3")]) and "dc-ce-01#link_down#3" in ddb.items)
send, ddb, ev = make()
check("sysName があれば device map より優先", send([iface("203.0.113.12", "eth0", "2", sysname="r2")]) and "r2#link_down#eth0" in ddb.items)
send, ddb, ev = make()
check("同じキーが 1 バッチに何度も出たら最後の状態だけ書く（down → up なら何も残らない）",
      send([iface("203.0.113.11", "eth1", 2), iface("203.0.113.11", "eth1", 1)]) == [] and ddb.items == {} and ev.calls == [])
send, ddb, ev = make()
check("同じキーが 1 バッチに何度も出ても開くのは 1 回（AnomalyOpened も 1 件）",
      len(send([iface("203.0.113.11", "eth1", 2), iface("203.0.113.11", "eth1", 2)])) == 1
      and sum("first_seen=:n" in c["UpdateExpression"] for c in ddb.calls) == 1 and ev.types() == ["AnomalyOpened"])
send, ddb, ev = make()
check("1 バッチの中は ts の順に並べて最後の状態を採る（collect の順は時刻の順ではない。up が先に来ても down(新) が勝つ）",
      len(send([iface("203.0.113.11", "eth1", 2, ts=20.0), iface("203.0.113.11", "eth1", 1, ts=10.0)])) == 1
      and ddb.plain("hq-ce-01#link_down#eth1")["status"] == "open")
check("逆に up(新) が後なら resolved",
      send([iface("203.0.113.11", "eth1", 1, ts=40.0), iface("203.0.113.11", "eth1", 2, ts=30.0)]) == []
      and ddb.plain("hq-ce-01#link_down#eth1")["status"] == "resolved")

# ---- trap
send, ddb, ev = make()
opened = send([trap("203.0.113.11", mod.LINK_DOWN, {".1.3.6.1.2.1.2.2.1.2.3": "eth3", ".1.3.6.1.2.1.2.2.1.1.3": 3})])
key = "hq-ce-01#link_down#eth3"
check("linkDown trap（MIB 無しの数値 OID）は ifDescr の varbind から target を取り source=trap",
      [o["anomaly_id"] for o in opened] == [key] and ddb.plain(key)["source"] == "trap" and ddb.plain(key)["detail"] == "eth3 is down (trap)")
check("linkUp trap で resolved", send([trap("203.0.113.11", mod.LINK_UP, {".1.3.6.1.2.1.2.2.1.2.3": "eth3"})]) == [] and ddb.plain(key)["status"] == "resolved")
send, ddb, ev = make()
send([{"measurement": "snmp_trap", "tags": {"source": "203.0.113.11", "oid": mod.LINK_DOWN, "name": "iso.3.6.1.6.3.1.1.5.3", "mib": ""},
       "fields": {"iso.3.6.1.2.1.2.2.1.2.38": "eth1", "iso.3.6.1.2.1.2.2.1.1.38": 38, "iso.3.6.1.2.1.1.3.0": 882671}}])
check("Telegraf 1.40 の \"iso.\" 始まりの数値 OID でも ifDescr を取る（source タグでも機器名が出る。2026-09-18 実機）",
      "hq-ce-01#link_down#eth1" in ddb.items and ddb.plain("hq-ce-01#link_down#eth1")["target"] == "eth1")
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

# ---- イベントが届かなかったとき（2026-09-24 のレビュー: 以前は put_events の失敗を見ず、開いた異常のワークフローが起きないままだった）
K1 = "hq-ce-01#link_down#eth1"
send, ddb, ev = make()
ev.fail = ["first"]
send([iface("203.0.113.11", "eth1", 2), iface("203.0.113.11", "eth2", 2)])
check("FailedEntryCount の entry だけ打ち直し、届いたら notified=true",
      [len(c) for c in ev.calls] == [2, 1] and sleeps == [2] and ev.calls[1][0] == ev.calls[0][0]
      and ddb.plain(K1)["notified"] is True and ddb.plain("hq-ce-01#link_down#eth2")["notified"] is True)
send, ddb, ev = make()
ev.fail = ["all", "all", "raise"]
opened = send([iface("203.0.113.11", "eth1", 2)])
check(f"{mod.EVENT_RETRIES} 回とも届かなければ落とさずに notified=false のまま残す（例外でもクエリを止めない）",
      len(opened) == 1 and len(ev.calls) == mod.EVENT_RETRIES and sleeps == [2, 4]
      and ddb.plain(K1)["status"] == "open" and ddb.plain(K1)["notified"] is False)
first = ddb.plain(K1)["first_seen"]
check("次のバッチで同じ down が来たら、開いたまま（first_seen はそのまま）で AnomalyOpened を出し直す",
      send([iface("203.0.113.11", "eth1", 2)]) == [] and len(ev.calls) == 4 and ev.calls[-1][0]["DetailType"] == "AnomalyOpened"
      and json.loads(ev.calls[-1][0]["Detail"])["first_seen"] == first and ddb.plain(K1)["notified"] is True)
check("届いたあとは出し直さない", send([iface("203.0.113.11", "eth1", 2)]) == [] and len(ev.calls) == 4)
ev.fail = ["all", "all", "all"]
send([iface("203.0.113.11", "eth1", 1)])
check("AnomalyResolved が届かなければ resolved で notified=false", ddb.plain(K1)["status"] == "resolved" and ddb.plain(K1)["notified"] is False)
n = len(ev.calls)
send([iface("203.0.113.11", "eth1", 1)])
check("次の up で AnomalyResolved を出し直し（resolved_at は最初のまま）、届いたら notified=true",
      len(ev.calls) == n + 1 and ev.calls[-1][0]["DetailType"] == "AnomalyResolved"
      and json.loads(ev.calls[-1][0]["Detail"])["resolved_at"] == ddb.plain(K1)["resolved_at"] and ddb.plain(K1)["notified"] is True)
check("そのあとの up は何も出さない", send([iface("203.0.113.11", "eth1", 1)]) == [] and len(ev.calls) == n + 1)
send, ddb, ev = make()
send([iface("203.0.113.11", "eth1", 2)])
del ddb.items[K1]["notified"]
check("notified の無い古い項目は届いたものとみなす（出し直さない）", send([iface("203.0.113.11", "eth1", 2)]) == [] and len(ev.calls) == 1)
send, ddb, ev = make()
ev.fail = ["all", "all", "all"]
send([iface("203.0.113.11", "eth1", 2)])
send([iface("203.0.113.11", "eth1", 1)])
check("届かなかった開きのあとで閉じたら、古い開きには印を付けない（AnomalyResolved の方で notified を見る）",
      ddb.plain(K1)["status"] == "resolved" and ddb.plain(K1)["notified"] is True and ev.types()[-1] == "AnomalyResolved")

# ---- trap の TTL（link 以外の trap には「直った」の知らせが無い）
clk = Clock()
send, ddb, ev = make(page=1, clock=clk)
T1, T2 = "hq-ce-01#trap#.1.3.6.1.6.3.1.1.5.5", "dc-ce-01#trap#.1.3.6.1.6.3.1.1.5.5"
send([trap("203.0.113.11", ".1.3.6.1.6.3.1.1.5.5", {}), trap("203.0.113.12", ".1.3.6.1.6.3.1.1.5.5", {}), iface("203.0.113.11", "eth1", 2)])
clk.t += mod.TRAP_TTL - 1
check("TRAP_TTL に満たなければ閉じない", send([]) == [] and ddb.plain(T1)["status"] == "open")
clk.t += 30
check("TRAP_SWEEP 秒たたないうちは見回らない", send([]) == [] and ddb.plain(T1)["status"] == "open" and len(ddb.queries) == 2)
clk.t += mod.TRAP_SWEEP
n = len(ev.calls)
send([])
check("最後の trap から TRAP_TTL 秒たった trap を resolved にし、AnomalyResolved（source=ttl）を出す。ページをまたいでも全部",
      ddb.plain(T1)["status"] == "resolved" and ddb.plain(T2)["status"] == "resolved"
      and sorted(d["anomaly_id"] for d in ev.details()[-2:]) == sorted([T1, T2]) and all(d["source"] == "ttl" for d in ev.details()[-2:])
      and ev.types()[-2:] == ["AnomalyResolved", "AnomalyResolved"] and len(ev.calls) == n + 1
      and ddb.queries[-1].get("ExclusiveStartKey") is not None and ddb.plain(T1)["notified"] is True)
check("link_down は TTL で閉じない（ポーリングの up で閉じる）", ddb.plain(K1)["status"] == "open")
check("見回りは status-last_seen-index を kind=trap で引く",
      ddb.queries[-1]["IndexName"] == mod.ANOMALY_INDEX == "status-last_seen-index" and ddb.queries[-1]["ExpressionAttributeValues"][":k"] == {"S": "trap"})
send([trap("203.0.113.11", ".1.3.6.1.6.3.1.1.5.5", {})])
check("閉じた trap がまた来たら開き直す（別の発生）", ddb.plain(T1)["status"] == "open" and ddb.plain(T1)["first_seen"] == clk.t)
# GSI は結果整合。見回りが古い last_seen を読んでも、本体の last_seen が新しければ閉じない
clk.t += mod.TRAP_TTL + mod.TRAP_SWEEP
stale = dict(ddb.items[T1])
send([trap("203.0.113.11", ".1.3.6.1.6.3.1.1.5.5", {})])
_query = ddb.query
ddb.query = lambda **kw: {"Items": [stale]}
clk.t += mod.TRAP_SWEEP
send([])
ddb.query = _query
check("GSI が古い行を返しても、本体の last_seen が新しければ閉じない", ddb.plain(T1)["status"] == "open")

# ---- 異常にしない trap と OID の形
check("coldStart / warmStart / nsNotifyShutdown / nsNotifyRestart は異常にしない（\"iso.\" でも \"1.\" でも）",
      all(mod.events({"name": "snmp_trap", "tags": {"agent_host": "203.0.113.11", "oid": o}, "fields": {}}, {}) == []
          for o in (".1.3.6.1.6.3.1.1.5.1", "iso.3.6.1.6.3.1.1.5.2", "1.3.6.1.4.1.8072.4.0.2", "iso.3.6.1.4.1.8072.4.0.3")))
check("\"iso.\" 始まりの linkDown の OID も linkDown として読む",
      mod.events({"name": "snmp_trap", "tags": {"agent_host": "203.0.113.11", "oid": "iso.3.6.1.6.3.1.1.5.3"}, "fields": {"ifDescr": "eth1"}}, {"203.0.113.11": "hq-ce-01"})
      == [("hq-ce-01", "link_down", "eth1", True, "trap")])
check("知らない trap は異常として開く（許可リストにしない）",
      mod.events({"name": "snmp_trap", "tags": {"agent_host": "203.0.113.11", "oid": "iso.3.6.1.4.1.9.9.41.2.0.1"}, "fields": {}}, {})[0][1:3]
      == ("trap", ".1.3.6.1.4.1.9.9.41.2.0.1"))
_src = open(SRC, encoding="utf-8").read()
check("detect は空のマイクロバッチでも sender を呼ぶ（TTL の見回りと出し直しを止めない）", 'if records or name == "detect":' in _src)

# ---- ログの経路: FRR の log file → lab の EC2 の /var/log/netops-lab/<機器名> → rsyslog → Telegraf の EC2 の socket_listener → Kafka の logs → Spark（2026-09-19）
def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()
frr_nodes = sorted(n[:-5] for n in os.listdir(os.path.join(ROOT, "lab", "frr")) if n.endswith(".conf") and n != "vtysh.conf")
check("FRR の 6 台とも log file と bgp log-neighbor-changes を持つ", len(frr_nodes) == 6 and all(
      re.search(r"^log file /var/log/frr/frr\.log informational$", _read("lab", "frr", n + ".conf"), re.M)
      and re.search(r"^\s*bgp log-neighbor-changes$", _read("lab", "frr", n + ".conf"), re.M) for n in frr_nodes))
clab = _read("lab", "wanlab.clab.yml.in")
check("containerlab は FRR の 6 台のログの置き場を bind する", all(f"- __LOG_DIR__/{n}:/var/log/frr" in clab for n in frr_nodes))
labsh = _read("lab", "lab.sh")
tele = _read("telegraf", "telegraf.conf.in")
tgsh = _read("telegraf", "telegraf.sh")
rsys = _read("lab", "rsyslog-frr.conf.in")
lab_locals = _read("terraform", "pipeline", "lab", "locals.tf")
log_dir = re.search(r"^LOG_DIR=(\S+)$", labsh, re.M).group(1)
check("lab.sh は render で __LOG_DIR__ を埋めて置き場を作り、logs で読める",
      '-e "s#__LOG_DIR__#$LOG_DIR#"' in labsh and 'install -d -m 1777 "$LOG_DIR/$n"' in labsh and re.search(r"^\s*logs\)", labsh, re.M) is not None)
check("置き場は src/ の外（user_data の s3 sync --delete に消されない）", log_dir.startswith("/var/log/"))
check("rsyslog は lab.sh と同じ置き場を読み、パスから機器名を取って Telegraf へ送る",
      'File="__LOG_DIR__/*/frr.log"' in rsys and 're_extract($!metadata!filename, "__LOG_DIR__/([^/]+)/frr[.]log"' in rsys
      and 'addMetadata="on"' in rsys and 'target="__TELEGRAF__" port="__LOG_PORT__" protocol="tcp"' in rsys
      and 'string="%$.dev% %msg%\\n"' in rsys)
check("lab.sh forward は rsyslog の設定の __*__ を全部埋める",
      all(k in labsh for k in ('"s#__LOG_DIR__#$LOG_DIR#g"', '"s#__TELEGRAF__#$t#"', '"s#__LOG_PORT__#$LOG_PORT#"', "rsyslog-frr.conf.in"))
      and set(re.findall(r"__[A-Z_]+__", rsys)) == {"__LOG_DIR__", "__TELEGRAF__", "__LOG_PORT__"})
# ログのポートは 4 か所で同じ（lab.sh / telegraf.sh / telegraf.conf.in / lab の SG）
log_port = re.search(r"^LOG_PORT=(\d+)$", labsh, re.M).group(1)
check("FRR のログのポートが lab.sh・telegraf.sh・telegraf.conf.in・lab の locals で同じ",
      re.search(rf"^LOG_PORT={log_port}$", tgsh, re.M) is not None and f'service_address = "tcp://:{log_port}"' in tele
      and re.search(rf"^\s*log_port\s*=\s*{log_port}$", lab_locals, re.M) is not None)
# 管理ネットワークは 3 か所で同じ（containerlab の mgmt / lab.sh / lab の locals の VPC ルート）
mgmt = re.search(r"^MGMT=(\S+)$", labsh, re.M).group(1)
check("管理ネットワークが containerlab・lab.sh・lab の locals で同じ",
      re.search(rf"^\s*ipv4-subnet: {re.escape(mgmt)}$", clab, re.M) is not None
      and re.search(rf'^\s*mgmt_cidr\s*=\s*"{re.escape(mgmt)}"$', lab_locals, re.M) is not None)
# ポーリング先は lab の定義から作る（lab/lab_topology.py --snmp-agents → snmp_agents.txt → telegraf.sh render が埋める）
_lt_spec = importlib.util.spec_from_file_location("lab_topology", os.path.join(ROOT, "lab", "lab_topology.py"))
lt = importlib.util.module_from_spec(_lt_spec); _lt_spec.loader.exec_module(lt)
_lab_devices, _ = lt.load(os.path.join(ROOT, "lab"))
_agents_line = lt.snmp_agents(_lab_devices)
_agents = re.findall(r"udp://([\d.]+):161", _agents_line)
check("Telegraf のポーリング先は lab の監視対象（enabled）の管理 IP で、全部管理ネットワークの中（VPC のルートで lab の EC2 へ行く）",
      len(_agents) == 4 and sorted(_agents) == sorted(d["mgmt_ip"] for d in _lab_devices if d["enabled"])
      and all(ipaddress.ip_address(a) in ipaddress.ip_network(mgmt) for a in _agents))
check("telegraf.conf.in の agents は __SNMP_AGENTS__ を telegraf.sh render が snmp_agents.txt で埋める（形を確かめてから）",
      re.search(r"^\s*agents = \[__SNMP_AGENTS__\]$", tele, re.M) is not None and 's#__SNMP_AGENTS__#$agents#' in tgsh
      and re.search(r"^AGENTS_FILE=snmp_agents\.txt$", tgsh, re.M) is not None
      and re.fullmatch(r'"udp://[0-9.]+:[0-9]+"(, *"udp://[0-9.]+:[0-9]+")*', _agents_line) is not None)
check("up.sh と lab の upload_telegraf_command は snmp_agents.txt を s3://<バケット>/telegraf/ に置く",
      'lab/lab_topology.py lab --snmp-agents' in _read("ops", "up.sh") and "/telegraf/snmp_agents.txt" in _read("ops", "up.sh")
      and "lab/lab_topology.py lab --snmp-agents" in _read("terraform", "pipeline", "lab", "outputs.tf"))
mgmt_gw = re.search(r"^MGMT_GW=(\S+)$", labsh, re.M).group(1)
_snmpd = [n for n in os.listdir(os.path.join(ROOT, "lab", "snmpd")) if n.endswith(".conf")]
check("snmpd の trap の宛先は lab.sh の MGMT_GW:162（forward が Telegraf へ DNAT する）",
      len(_snmpd) == 4 and all(re.search(rf"^trap2sink {re.escape(mgmt_gw)} \S+ 162$", _read("lab", "snmpd", n), re.M) for n in _snmpd))
check("lab.sh up は毎回 forward を呼び、forward / forward-status がある",
      '"$SELF" forward' in labsh and re.search(r"^\s*forward\)", labsh, re.M) is not None and re.search(r"^\s*forward-status\)", labsh, re.M) is not None)
check("forward の iptables の規則は全部目印付き（unforward で消せる）",
      all("${c[@]}" in l for l in labsh.splitlines() if re.match(r"\s*iptables .*-I ", l)))
check("Telegraf は FRR のログを socket_listener で受け、frr_log として logs トピックに出す",
      'name_override = "frr_log"' in tele and "[[inputs.tail]]" not in tele
      and re.search(r'topic = "logs"[\s\S]*?namepass = \["frr_log"\]|namepass = \["frr_log"\][\s\S]*?topic = "logs"', tele) is not None)
check("metrics / traps の出力に frr_log が混ざらない（namepass / namedrop）",
      all(re.search(r"name(pass|drop)", blk) for blk in tele.split("[[outputs.kafka]]")[1:]))
check("行の先頭の機器名を sysName のタグにする（detect と同じ機器名のタグ）", "%{NOTSPACE:sysName:tag} " in tele)
# grok と同じ形を Python の正規表現で確かめる（rsyslog が機器名を付けた FRR の log file の 1 行）
_line = "hq-ce-01 2026/09/18 01:02:03 BGP: [M59KS-A3ZXZ] bgp_update_receive: rcvd End-of-RIB for IPv4 Unicast from 203.0.113.2 in vrf default"
_m = re.match(r"^(?P<sysName>\S+) (?P<log_time>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) (?P<daemon>\w+): (?P<message>.*)$", _line)
check("grok の形（機器名 日時 デーモン: 本文）が rsyslog の送る行に合う",
      _m is not None and _m.group("sysName") == "hq-ce-01" and _m.group("daemon") == "BGP"
      and "%{NOTSPACE:sysName:tag} %{FRR_TS:log_time} %{WORD:daemon:tag}: %{GREEDYDATA:message}" in tele)
check("detect は frr_log を異常にしない", mod.events({"name": "frr_log", "tags": {"sysName": "hq-ce-01"}, "fields": {"message": "x"}}, {}) == [])
_access = _read("terraform", "pipeline", "stream", "access.tf")
_lab_tg = _read("terraform", "pipeline", "lab", "telegraf.tf")
check("stream の stream_produce は Telegraf が lab の state に無くても role が空にならない（down.sh の destroy が検証で止まらない。2026-09-19）",
      'role = coalesce(local.telegraf_role_name, "${local.name_prefix}-telegraf")' in _access
      and re.search(r'resource "aws_iam_role" "telegraf" \{[\s\S]*?name\s+= "\$\{local\.name_prefix\}-telegraf"', _lab_tg) is not None)
sink_tf = _read("terraform", "pipeline", "stream", "sink.tf")
check("S3 sink と Spark の既定は logs も読む", '"metrics,traps,logs"' in sink_tf and mod.LOG_TOPICS == "traps,logs"
      and mod.sink_topics("opensearch", mod.METRIC_TOPICS, mod.LOG_TOPICS) == "traps,logs")

print(f"通過 {passed} / 失敗 0")
