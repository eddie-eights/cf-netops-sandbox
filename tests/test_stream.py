"""異常検知（spark/snmp_sinks.py の detect）の模擬テスト。Neptune の Gremlin（HTTP の POST）と EventBridge を差し替えて、
異常の「いま」（Neptune の anomaly の頂点）と履歴（S3 Tables の anomaly_events に渡す行）とイベントを確かめ、
terraform/pipeline/stream に detector Lambda も DynamoDB の異常テーブルも無いことも確かめる（2026-09-24 に DynamoDB をやめた）。
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

# ---- terraform/pipeline/stream: detector Lambda は analytics の Spark に寄せ、異常の「いま」は Neptune に置く
TF_DIR = os.path.join(ROOT, "terraform", "pipeline", "stream")
tf = ""
for name in sorted(os.listdir(TF_DIR)):
    if name.endswith(".tf"):
        with open(os.path.join(TF_DIR, name), encoding="utf-8") as f:
            tf += f.read() + "\n"
check("stream/detector.py は無い（検知は spark/snmp_sinks.py）", not os.path.exists(os.path.join(ROOT, "stream", "detector.py")))
check("terraform/pipeline/stream に detector の Lambda が無い", '"detector"' not in tf and "stream/detector.py" not in tf and "archive_file" not in tf)
check("terraform/pipeline/stream に lambda のエンドポイントが無い", '.lambda"' not in tf)
check("terraform/pipeline/stream に MSK Connect の S3 sink が無い（Spark が S3 Tables に入れるので 2026-09-26 に削除。sts のエンドポイントも一緒に）",
      not os.path.exists(os.path.join(TF_DIR, "sink.tf")) and "mskconnect" not in tf and "create_s3_sink" not in tf
      and "kafkaconnect" not in tf and "create_sts_endpoint" not in tf and "aws_vpc_endpoint" not in tf)
check("terraform/pipeline/stream に DynamoDB が無い（異常の「いま」は Neptune、履歴は S3 Tables。2026-09-24）",
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
check("argparse に --neptune-endpoint / --anomaly-events-table / --device-map / --event-bus / --event-source があり、--anomaly-table は無い",
      all(f'"--{a}"' in src for a in ("neptune-endpoint", "anomaly-events-table", "device-map", "event-bus", "event-source"))
      and '"--anomaly-table"' not in src and 'client("dynamodb")' not in src and "update_item" not in src)
check("build は --neptune-endpoint があるときだけ detect のクエリを足し、履歴の書き手（S3 Tables）を渡す",
      re.search(r'if args\.neptune_endpoint:[\s\S]*?NeptuneAnomalies\([\s\S]*?make_history_writer\(spark, args\.anomaly_events_table\)[\s\S]*?http_query\(rows, "detect"', src) is not None)
BASE = ["--bootstrap", "b", "--checkpoint", "c", "--sinks", "iceberg", "--iceberg-table", "cat.ns.t"]
check("Source の既定は netops.spark（terraform は <接頭辞>.spark を渡す）、DetailType は AnomalyOpened / AnomalyResolved",
      mod.EVENT_SOURCE == "netops.spark" and mod.EVENT_DETAIL_TYPE == "AnomalyOpened" and mod.EVENT_RESOLVED_TYPE == "AnomalyResolved"
      and mod.parse_args(BASE).event_source == "netops.spark")
try:
    import contextlib, io
    with contextlib.redirect_stderr(io.StringIO()):
        mod.parse_args(BASE + ["--neptune-endpoint", "n:8182"])
    _refused = False
except SystemExit:
    _refused = True
check("--neptune-endpoint だけで --anomaly-events-table が無ければ引数の段階で止める（履歴を残さずに検知しない）",
      _refused and mod.parse_args(BASE + ["--neptune-endpoint", "n:8182", "--anomaly-events-table", "s3tables.netops.anomaly_events"]).neptune_endpoint == "n:8182")
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

# ---- Gremlin のリテラルと GraphSON
check("gremlin_literal は \\ と ' と改行・タブ・制御文字を逃がす（Neptune の文字列の Gremlin は生の改行を受け付けない）",
      mod.gremlin_literal("a'b\\c\nd\re\tf\x01g") == r"'a\'b\\c\nd\re\tf\u0001g'"
      and mod.gremlin_literal(True) == "true" and mod.gremlin_literal(12) == "12" and mod.gremlin_literal("日本") == "'日本'")
check("graphson は g:List / g:Map / g:Int64 / g:T を素の値にする",
      mod.graphson({"@type": "g:List", "@value": [{"@type": "g:Map", "@value": [
          {"@type": "g:T", "@value": "id"}, "k", "n", {"@type": "g:Int64", "@value": 5}, "b", False]}]}) == [{"id": "k", "n": 5, "b": False}])


# ---- Neptune の模擬（NeptuneAnomalies が組む 4 つの形の Gremlin だけを読む）
def lit(s, i):
    """s[i:] の先頭のリテラルを読んで (値, 次の位置)"""
    if s[i] == "'":
        out, i = [], i + 1
        while s[i] != "'":
            if s[i] == "\\":
                c = s[i + 1]
                if c == "u":
                    out.append(chr(int(s[i + 2:i + 6], 16))); i += 6; continue
                out.append({"n": "\n", "r": "\r", "t": "\t"}.get(c, c)); i += 2; continue
            assert s[i] not in "\n\r", "生の改行"
            out.append(s[i]); i += 1
        return "".join(out), i + 1
    m = re.compile(r"true|false|-?\d+(\.\d+)?").match(s, i)
    v = m.group(0)
    return (v == "true") if v in ("true", "false") else (float(v) if "." in v else int(v)), m.end()


def lits(s):
    out, i = [], 0
    while i < len(s):
        if s[i] in "',":
            if s[i] == ",":
                i += 1; continue
            v, i = lit(s, i); out.append(v)
        else:
            v, i = lit(s, i); out.append(v)
    return out


class FakeNeptune:
    """頂点は {id: {property: 値}}。queries に打たれた Gremlin を残す"""
    def __init__(self):
        self.v = {}
        self.queries = []

    def em(self, k):
        return {"id": k, "label": "anomaly", **self.v[k]}

    def __call__(self, g):
        self.queries.append(g)
        m = re.fullmatch(r"g\.V\((.*)\)\.hasLabel\('anomaly'\)\.elementMap\(\)", g)
        if m:
            return [self.em(k) for k in lits(m.group(1)) if k in self.v]
        m = re.fullmatch(r"g\.V\((.+?)\)\.fold\(\)\.coalesce\(unfold\(\),addV\('anomaly'\)\.property\(id,(.+?)\)\)"
                         r"(\.sideEffect\(properties\('resolved_at'\)\.drop\(\)\))?((?:\.property\(single,.+?\))*)\.id\(\)", g)
        if m:
            k = lits(m.group(1))[0]
            assert lits(m.group(2))[0] == k
            item = self.v.setdefault(k, {})
            if m.group(3):
                item.pop("resolved_at", None)
            body, i = m.group(4), 0
            while i < len(body):
                assert body.startswith(".property(single,", i), body[i:]
                n, i = lit(body, i + len(".property(single,"))
                assert body[i] == ","
                val, i = lit(body, i + 1)
                assert body[i] == ")"
                i += 1
                item[n] = val
            return [k]
        m = re.fullmatch(r"g\.V\((.+?)\)\.hasLabel\('anomaly'\)\.has\('status',(.+?)\)\.has\((.+?),(.+?)\)\.property\(single,'notified',true\)\.id\(\)", g)
        if m:
            k, st, f, val = (lits(x)[0] for x in m.groups())
            it = self.v.get(k)
            if it and it.get("status") == st and it.get(f) == val:
                it["notified"] = True
                return [k]
            return []
        m = re.fullmatch(r"g\.V\(\)\.hasLabel\('anomaly'\)\.has\('status','open'\)\.has\('kind','trap'\)\.has\('last_seen',lt\((\d+)\)\)\.elementMap\(\)", g)
        if m:
            cut = int(m.group(1))
            return [self.em(k) for k, it in sorted(self.v.items()) if it.get("status") == "open" and it.get("kind") == "trap" and it.get("last_seen", 0) < cut]
        raise AssertionError("知らない Gremlin: " + g)

    def plain(self, key):
        return dict(self.v[key])


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


class History(list):
    """履歴の書き手（S3 Tables の append）の模擬。1 回の呼び出しの行をまとめて足し、呼ばれた順に order へ残す"""
    def __init__(self, order):
        super().__init__()
        self.order = order
    def __call__(self, rows):
        assert rows, "空の append はしない"
        self.order.append(("history", [r["event_id"] for r in rows]))
        self.extend(rows)


def make(clock=None):
    nep, ev = FakeNeptune(), FakeEvents()
    order = []
    hist = History(order)
    def post(g):
        if ".property(single," in g and "notified',true" not in g:
            order.append(("neptune", g))
        return nep(g)
    sleeps.clear()
    send = mod.make_detect_sender(mod.NeptuneAnomalies("n:8182", "ap-northeast-1", post=post), hist,
                                  mod.parse_device_map("203.0.113.11=hq-ce-01,203.0.113.12=dc-ce-01"), "ap-northeast-1", "default",
                                  "demo-poc.spark", events_client=ev, clock=clock or Clock(), sleep=sleeps.append)
    return send, nep, ev, hist


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
send, nep, ev, hist = make()
check("正常なポーリング（up）は何も書かない", send([iface("203.0.113.11", "eth1", 1)]) == [] and nep.v == {} and ev.calls == [] and hist == [])
check("lo は見ない", send([iface("203.0.113.11", "lo", 2)]) == [] and nep.v == {})
check("dict でない行や関係ない measurement は捨てる",
      send([None, "garbage", {"measurement": "cpu", "tags": {}, "fields": {"usage": 1}}, {"measurement": "interface", "tags": {}, "fields": {}}]) == []
      and nep.v == {})

opened = send([iface("203.0.113.11", "eth1", 2)])
key = "hq-ce-01#link_down#eth1"
row = nep.plain(key)
check("down → open（device_id / kind / target / source=poll / detail / first_seen=last_seen）",
      row["status"] == "open" and row["device_id"] == "hq-ce-01" and row["kind"] == "link_down" and row["target"] == "eth1"
      and row["source"] == "poll" and row["detail"] == "eth1 is down (poll)" and row["first_seen"] == row["last_seen"])
check("Neptune の書き込みは property(single, …)（既定の set だと値が積み上がる）",
      all(".property(" not in q.replace(".property(single,", "").replace(".property(id,", "") for q in nep.queries))
check("新しく open になったものだけ返し、AnomalyOpened を 1 件出す",
      [o["anomaly_id"] for o in opened] == [key] and len(ev.calls) == 1 and len(ev.calls[0]) == 1)
entry = ev.calls[0][0]
detail = json.loads(entry["Detail"])
check("put_events の Source（--event-source がそのまま入る）/ DetailType / EventBusName", entry["Source"] == "demo-poc.spark" and entry["DetailType"] == "AnomalyOpened" and entry["EventBusName"] == "default")
check("Detail に anomaly_id / device_id / kind / target / first_seen / detail / source",
      detail == {"anomaly_id": key, "device_id": "hq-ce-01", "kind": "link_down", "target": "eth1", "first_seen": row["first_seen"],
                 "detail": "eth1 is down (poll)", "source": "poll"})
first = row["first_seen"]
check("開いたら履歴に opened を 1 行（event_id / occurrence_id は <anomaly_id>#<first_seen> から。resolved_at は空）",
      len(hist) == 1 and hist[0] == {"event_id": f"{key}#{first}#opened", "anomaly_id": key, "occurrence_id": f"{key}#{first}", "event": "opened",
                                     "device_id": "hq-ce-01", "kind": "link_down", "target": "eth1", "source": "poll",
                                     "detail": "eth1 is down (poll)", "first_seen": first, "resolved_at": None, "event_time": first})
check("履歴の列は tables.tf の anomaly_events と同じ", [c for c, _ in mod.ANOMALY_EVENT_COLUMNS] == list(hist[0]))

nep.v[key]["first_seen"] = first - 100   # 前から開いていたことにする
check("開いたままの down は first_seen を残し、イベントも履歴も出さない",
      send([iface("203.0.113.11", "eth1", 2)]) == [] and nep.plain(key)["first_seen"] == first - 100 and len(ev.calls) == 1 and len(hist) == 1)
check("up → resolved（resolved_at が付く）",
      send([iface("203.0.113.11", "eth1", 1)]) == [] and nep.plain(key)["status"] == "resolved" and "resolved_at" in nep.plain(key))
entry = ev.calls[-1][0]
check("open → resolved で AnomalyResolved を 1 件出す（Detail に anomaly_id / device_id / kind / target / resolved_at / source）",
      len(ev.calls) == 2 and len(ev.calls[1]) == 1 and entry["DetailType"] == "AnomalyResolved" and entry["Source"] == "demo-poc.spark"
      and json.loads(entry["Detail"]) == {"anomaly_id": key, "device_id": "hq-ce-01", "kind": "link_down", "target": "eth1",
                                          "resolved_at": nep.plain(key)["resolved_at"], "source": "poll"})
check("閉じたら履歴に resolved を 1 行（同じ発生の occurrence_id、resolved_at 付き）",
      len(hist) == 2 and hist[1]["event"] == "resolved" and hist[1]["occurrence_id"] == f"{key}#{first - 100}"
      and hist[1]["event_id"] == f"{key}#{first - 100}#resolved" and hist[1]["resolved_at"] == nep.plain(key)["resolved_at"])
check("resolved のあとの up は何もしない（AnomalyResolved も履歴も出さない）",
      send([iface("203.0.113.11", "eth1", 1)]) == [] and nep.plain(key)["status"] == "resolved" and len(ev.calls) == 2 and len(hist) == 2)
reopened = send([iface("203.0.113.11", "eth1", 2)])
check("resolved → 再 open はイベントをもう一度出し、first_seen を今にして resolved_at を消す（worker が起こし直せるように。2026-09-18）",
      len(reopened) == 1 and reopened[0]["first_seen"] >= first and nep.plain(key)["status"] == "open"
      and nep.plain(key)["first_seen"] == reopened[0]["first_seen"] and "resolved_at" not in nep.plain(key)
      and len(ev.calls) == 3 and ev.calls[-1][0]["DetailType"] == "AnomalyOpened" and hist[-1]["event"] == "opened")

send, nep, ev, hist = make()
send([iface("203.0.113.11", "eth1", 2), iface("203.0.113.12", "eth2", 2)])
check("履歴は Neptune より先に書く（落ちたら同じバッチを読み直すので、証跡から開閉が抜けない）",
      [o[0] for o in hist.order][:1] == ["history"] and len([o for o in hist.order if o[0] == "history"]) == 1)
send, nep, ev, hist = make()
check("ifDescr が無ければ ifIndex", send([iface("203.0.113.12", None, 2, ifindex="3")]) and "dc-ce-01#link_down#3" in nep.v)
send, nep, ev, hist = make()
check("sysName があれば device map より優先", send([iface("203.0.113.12", "eth0", "2", sysname="r2")]) and "r2#link_down#eth0" in nep.v)
send, nep, ev, hist = make()
check("同じキーが 1 バッチに何度も出たら最後の状態だけ書く（down → up なら何も残らない）",
      send([iface("203.0.113.11", "eth1", 2), iface("203.0.113.11", "eth1", 1)]) == [] and nep.v == {} and ev.calls == [] and hist == [])
send, nep, ev, hist = make()
check("同じキーが 1 バッチに何度も出ても開くのは 1 回（AnomalyOpened も履歴も 1 件）",
      len(send([iface("203.0.113.11", "eth1", 2), iface("203.0.113.11", "eth1", 2)])) == 1
      and sum("'first_seen'" in q and "addV" in q for q in nep.queries) == 1 and ev.types() == ["AnomalyOpened"] and len(hist) == 1)
send, nep, ev, hist = make()
check("1 バッチの中は ts の順に並べて最後の状態を採る（collect の順は時刻の順ではない。up が先に来ても down(新) が勝つ）",
      len(send([iface("203.0.113.11", "eth1", 2, ts=20.0), iface("203.0.113.11", "eth1", 1, ts=10.0)])) == 1
      and nep.plain("hq-ce-01#link_down#eth1")["status"] == "open")
check("逆に up(新) が後なら resolved",
      send([iface("203.0.113.11", "eth1", 1, ts=40.0), iface("203.0.113.11", "eth1", 2, ts=30.0)]) == []
      and nep.plain("hq-ce-01#link_down#eth1")["status"] == "resolved")
send, nep, ev, hist = make()
send([iface("203.0.113.11", f"eth{i}", 1) for i in range(mod.NEPTUNE_IDS_PER_QUERY + 5)])
check(f"キーが {mod.NEPTUNE_IDS_PER_QUERY} を超えたら g.V(…) を分けて読む",
      sum(q.endswith(".hasLabel('anomaly').elementMap()") and q.startswith("g.V('") for q in nep.queries) == 2)

# ---- trap
send, nep, ev, hist = make()
opened = send([trap("203.0.113.11", mod.LINK_DOWN, {".1.3.6.1.2.1.2.2.1.2.3": "eth3", ".1.3.6.1.2.1.2.2.1.1.3": 3})])
key = "hq-ce-01#link_down#eth3"
check("linkDown trap（MIB 無しの数値 OID）は ifDescr の varbind から target を取り source=trap",
      [o["anomaly_id"] for o in opened] == [key] and nep.plain(key)["source"] == "trap" and nep.plain(key)["detail"] == "eth3 is down (trap)")
check("linkUp trap で resolved", send([trap("203.0.113.11", mod.LINK_UP, {".1.3.6.1.2.1.2.2.1.2.3": "eth3"})]) == [] and nep.plain(key)["status"] == "resolved"
      and hist[-1]["source"] == "trap")
send, nep, ev, hist = make()
send([{"measurement": "snmp_trap", "tags": {"source": "203.0.113.11", "oid": mod.LINK_DOWN, "name": "iso.3.6.1.6.3.1.1.5.3", "mib": ""},
       "fields": {"iso.3.6.1.2.1.2.2.1.2.38": "eth1", "iso.3.6.1.2.1.2.2.1.1.38": 38, "iso.3.6.1.2.1.1.3.0": 882671}}])
check("Telegraf 1.40 の \"iso.\" 始まりの数値 OID でも ifDescr を取る（source タグでも機器名が出る。2026-09-18 実機）",
      "hq-ce-01#link_down#eth1" in nep.v and nep.plain("hq-ce-01#link_down#eth1")["target"] == "eth1")
send([trap("203.0.113.11", mod.LINK_DOWN, {"ifDescr": "eth4", "ifIndex": 4})])
check("MIB がある varbind 名（ifDescr）でも取れる", "hq-ce-01#link_down#eth4" in nep.v)
send, nep, ev, hist = make()
send([trap("203.0.113.11", mod.LINK_DOWN, {"ifIndex.5": 5})])
check("ifDescr が無い trap は ifIndex", "hq-ce-01#link_down#5" in nep.v)
send, nep, ev, hist = make()
opened = send([trap("203.0.113.11", ".1.3.6.1.6.3.1.1.5.5", {})])
key = "hq-ce-01#trap#.1.3.6.1.6.3.1.1.5.5"
check("linkDown / linkUp 以外の trap は kind=trap で open", key in nep.v and nep.plain(key)["kind"] == "trap" and nep.plain(key)["detail"] == "trap .1.3.6.1.6.3.1.1.5.5"
      and opened[0]["kind"] == "trap")
send, nep, ev, hist = make()
odd = "eth'1\\x\ny"
send([iface("203.0.113.11", odd, 2)])
check("インタフェース名に ' や \\ や改行があっても Gremlin が壊れず、そのまま戻る", nep.plain(f"hq-ce-01#link_down#{odd}")["target"] == odd)

# ---- PutEvents の 10 件制限
send, nep, ev, hist = make()
opened = send([iface("203.0.113.11", f"eth{i}", 2) for i in range(23)])
check("新しい異常が 10 件を超えたら put_events を分ける", len(opened) == 23 and [len(c) for c in ev.calls] == [10, 10, 3] and len(hist) == 23)

# ---- イベントが届かなかったとき（2026-09-24 のレビュー: 以前は put_events の失敗を見ず、開いた異常のワークフローが起きないままだった）
K1 = "hq-ce-01#link_down#eth1"
send, nep, ev, hist = make()
ev.fail = ["first"]
send([iface("203.0.113.11", "eth1", 2), iface("203.0.113.11", "eth2", 2)])
check("FailedEntryCount の entry だけ打ち直し、届いたら notified=true",
      [len(c) for c in ev.calls] == [2, 1] and sleeps == [2] and ev.calls[1][0] == ev.calls[0][0]
      and nep.plain(K1)["notified"] is True and nep.plain("hq-ce-01#link_down#eth2")["notified"] is True)
send, nep, ev, hist = make()
ev.fail = ["all", "all", "raise"]
opened = send([iface("203.0.113.11", "eth1", 2)])
check(f"{mod.EVENT_RETRIES} 回とも届かなければ落とさずに notified=false のまま残す（例外でもクエリを止めない）",
      len(opened) == 1 and len(ev.calls) == mod.EVENT_RETRIES and sleeps == [2, 4]
      and nep.plain(K1)["status"] == "open" and nep.plain(K1)["notified"] is False)
first = nep.plain(K1)["first_seen"]
check("次のバッチで同じ down が来たら、開いたまま（first_seen はそのまま）で AnomalyOpened を出し直す（履歴は足さない）",
      send([iface("203.0.113.11", "eth1", 2)]) == [] and len(ev.calls) == 4 and ev.calls[-1][0]["DetailType"] == "AnomalyOpened"
      and json.loads(ev.calls[-1][0]["Detail"])["first_seen"] == first and nep.plain(K1)["notified"] is True and len(hist) == 1)
check("届いたあとは出し直さない", send([iface("203.0.113.11", "eth1", 2)]) == [] and len(ev.calls) == 4)
ev.fail = ["all", "all", "all"]
send([iface("203.0.113.11", "eth1", 1)])
check("AnomalyResolved が届かなければ resolved で notified=false", nep.plain(K1)["status"] == "resolved" and nep.plain(K1)["notified"] is False)
n = len(ev.calls)
send([iface("203.0.113.11", "eth1", 1)])
check("次の up で AnomalyResolved を出し直し（resolved_at は最初のまま）、届いたら notified=true",
      len(ev.calls) == n + 1 and ev.calls[-1][0]["DetailType"] == "AnomalyResolved"
      and json.loads(ev.calls[-1][0]["Detail"])["resolved_at"] == nep.plain(K1)["resolved_at"] and nep.plain(K1)["notified"] is True
      and len(hist) == 2)
check("そのあとの up は何も出さない", send([iface("203.0.113.11", "eth1", 1)]) == [] and len(ev.calls) == n + 1)
send, nep, ev, hist = make()
send([iface("203.0.113.11", "eth1", 2)])
del nep.v[K1]["notified"]
check("notified の無い古い頂点は届いたものとみなす（出し直さない）", send([iface("203.0.113.11", "eth1", 2)]) == [] and len(ev.calls) == 1)
send, nep, ev, hist = make()
ev.fail = ["all", "all", "all"]
send([iface("203.0.113.11", "eth1", 2)])
send([iface("203.0.113.11", "eth1", 1)])
check("届かなかった開きのあとで閉じたら、古い開きには印を付けない（AnomalyResolved の方で notified を見る）",
      nep.plain(K1)["status"] == "resolved" and nep.plain(K1)["notified"] is True and ev.types()[-1] == "AnomalyResolved")

# ---- trap の TTL（link 以外の trap には「直った」の知らせが無い）
clk = Clock()
send, nep, ev, hist = make(clock=clk)
T1, T2 = "hq-ce-01#trap#.1.3.6.1.6.3.1.1.5.5", "dc-ce-01#trap#.1.3.6.1.6.3.1.1.5.5"
send([trap("203.0.113.11", ".1.3.6.1.6.3.1.1.5.5", {}), trap("203.0.113.12", ".1.3.6.1.6.3.1.1.5.5", {}), iface("203.0.113.11", "eth1", 2)])
sweeps = lambda: sum("has('kind','trap')" in q for q in nep.queries)
clk.t += mod.TRAP_TTL - 1
check("TRAP_TTL に満たなければ閉じない", send([]) == [] and nep.plain(T1)["status"] == "open")
clk.t += 30
check("TRAP_SWEEP 秒たたないうちは見回らない", send([]) == [] and nep.plain(T1)["status"] == "open" and sweeps() == 2)
clk.t += mod.TRAP_SWEEP
n = len(ev.calls)
send([])
check("最後の trap から TRAP_TTL 秒たった trap を resolved にし、AnomalyResolved（source=ttl）を出す",
      nep.plain(T1)["status"] == "resolved" and nep.plain(T2)["status"] == "resolved"
      and sorted(d["anomaly_id"] for d in ev.details()[-2:]) == sorted([T1, T2]) and all(d["source"] == "ttl" for d in ev.details()[-2:])
      and ev.types()[-2:] == ["AnomalyResolved", "AnomalyResolved"] and len(ev.calls) == n + 1 and nep.plain(T1)["notified"] is True)
check("TTL で閉じたものも履歴に resolved（source=ttl）", sorted(r["anomaly_id"] for r in hist[-2:]) == sorted([T1, T2])
      and all(r["event"] == "resolved" and r["source"] == "ttl" for r in hist[-2:]))
check("link_down は TTL で閉じない（ポーリングの up で閉じる）", nep.plain(K1)["status"] == "open")
send([trap("203.0.113.11", ".1.3.6.1.6.3.1.1.5.5", {})])
check("閉じた trap がまた来たら開き直す（別の発生）", nep.plain(T1)["status"] == "open" and nep.plain(T1)["first_seen"] == clk.t)
clk.t += mod.TRAP_TTL + mod.TRAP_SWEEP
send([trap("203.0.113.11", ".1.3.6.1.6.3.1.1.5.5", {})])
check("見回りと同じバッチに trap が来たキーは閉じない（来た trap で last_seen が進む）",
      nep.plain(T1)["status"] == "open" and nep.plain(T1)["last_seen"] == clk.t and hist[-1]["event"] == "opened")

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
# ログのポートは 4 か所で同じ（lab.sh / telegraf.sh / telegraf.conf.in / lab の locals）
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
check("Spark の既定は logs も読む", mod.LOG_TOPICS == "traps,logs"
      and mod.sink_topics("opensearch", mod.METRIC_TOPICS, mod.LOG_TOPICS) == "traps,logs")

print(f"通過 {passed} / 失敗 0")
