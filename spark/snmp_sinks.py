"""Kafka（MSK、IAM 認証）のトピックを読み、選んだ格納先に流し続け、異常を検知して EventBridge に出す Spark Structured Streaming のジョブ（Kafka の 4 分岐のうち Spark の 3 本 + 検知）。

EMR Serverless の上で動く（terraform/pipeline/analytics）。起動は ops/up.sh の a-3（start-job-run）で、引数は terraform/pipeline/analytics の
output job_driver_json が組み立てる（--bootstrap / --checkpoint / --sinks と、格納先ごとの --iceberg-table などの値）。
Kafka と S3 Tables の jar、カタログの設定は spark-submit の --conf で渡す。

格納先は 3 つ（--sinks にカンマ区切り。4 つ目の Splunk は Kafka の sink（MSK Connect）にする予定で後回し）:
  iceberg     全トピック → S3 Tables（Iceberg）のテーブルに append（履歴の正本）
  opensearch  ログのトピックだけ → OpenSearch Serverless（TIMESERIES 型のコレクション）の _bulk に SigV4 で POST
  prometheus  メトリクスのトピックだけ → Amazon Managed Service for Prometheus の remote write に SigV4 で POST（数値の field だけ）
どのトピックがメトリクスでどれがログかは --metric-topics / --log-topics（既定は Telegraf の metrics と traps,logs。
logs は FRR のログ。lab の EC2 の rsyslog が Telegraf の EC2 へ送る）。格納先ごとに別のストリーミングクエリ（別の Kafka の購読と checkpoint）に
する。1 つが止まったらジョブを 1 で終わらせ、EMR Serverless に起こし直させる（どのクエリも checkpoint の続きから読む）。

Telegraf の JSON 出力（outputs.kafka の data_format = "json"、json_timestamp_units = "1s"）は
  {"fields": {…}, "name": "<measurement>", "tags": {"agent_host": "…", "host": "…", …}, "timestamp": <秒>}
の形。列に分けるのは timestamp / name / agent_host / host だけで、tags と fields は JSON 文字列のまま入れる
（機器やメトリクスが増えてもテーブルの列を変えないため。terraform/pipeline/analytics/tables.tf の列と同じ）。

異常の検知（detect）は格納先とは別に常に動く 4 本目のクエリ（検知したら EventBridge にイベントを出す）:
  metrics の interface で ifOperStatus が down のインタフェース（ポーリング）と、traps の linkDown（即時）を DynamoDB の異常テーブル
  （terraform/pipeline/stream の anomalies。キーは <機器>#<種別>#<インタフェース>）に open で書き、up に戻ったポーリングと linkUp で resolved にする。
  新しく open になったときだけ EventBridge の既定のバスに Source <接頭辞>.spark（--event-source）/ DetailType AnomalyOpened を put_events する
  （terraform/workflow の events.tf がルールで SQS に流し、Temporal の worker が調査ワークフローを起こす）。resolved にしたときは AnomalyResolved。
  イベントが届いたかは項目の notified に残し、届かなかったものは次のバッチで出し直す。link 以外の trap は TRAP_TTL 秒 次の trap が来なければ
  resolved にする（「直った」の trap が無いので）。coldStart / warmStart と snmpd の停止・再起動の知らせ（IGNORED_TRAPS）は異常にしない。
  機器名は sysName タグ（小文字・ドメイン無しに揃える）> --device-map（別名=機器名,...。ops/up.sh が lab の定義から作る）の順で引く。以前 terraform/pipeline/stream の detector Lambda がしていたことをここに寄せた。

HTTP の送信は driver でまとめて行う（マイクロバッチを collect する。PoC の量（機器数台、10 秒間隔）なら 1 分に数百行）。
量が増えたら foreachPartition に移す。remote write の protobuf と snappy は外部ライブラリ無しで組む
（EMR Serverless の Python に protobuf / python-snappy は無い。snappy は「全部リテラル」の圧縮で規格上正しい）。
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.request

METRIC_TOPICS = "metrics"   # Telegraf の inputs.snmp（telegraf/telegraf.conf.in。Telegraf の EC2 で動く）
LOG_TOPICS = "traps,logs"   # traps = Telegraf の inputs.snmp_trap、logs = inputs.socket_listener（FRR のログ。measurement は frr_log）
SINKS = ("iceberg", "opensearch", "prometheus")
TRIGGER = "60 seconds"
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3        # 5xx と接続エラーだけ打ち直す。4xx は捨ててログに出す（古すぎるサンプルなどは何度打っても通らない）
BULK_SIZE = 500         # 1 回の POST に載せる行数
OPENSEARCH_INDEX = "snmp-logs"
METRIC_PREFIX = "snmp"


# ---------------------------------------------------------------- 引数
def parse_args(argv):
    p = argparse.ArgumentParser(prog="snmp_sinks.py", description=__doc__.split("\n")[0])
    p.add_argument("--bootstrap", required=True, help="MSK の bootstrap servers（SASL/IAM、9098）")
    p.add_argument("--checkpoint", required=True, help="checkpoint の親（s3://<バケット>/analytics/checkpoint/。格納先ごとに下にディレクトリを切る）")
    p.add_argument("--sinks", required=True, help="格納先（カンマ区切り。iceberg / opensearch / prometheus）")
    p.add_argument("--region", default="ap-northeast-1", help="SigV4 のリージョン")
    p.add_argument("--metric-topics", default=METRIC_TOPICS, help="メトリクスのトピック（カンマ区切り。iceberg と prometheus が読む）")
    p.add_argument("--log-topics", default=LOG_TOPICS, help="ログのトピック（カンマ区切り。iceberg と opensearch が読む）")
    p.add_argument("--iceberg-table", default="", help="iceberg: catalog.namespace.table")
    p.add_argument("--opensearch-endpoint", default="", help="opensearch: コレクションのエンドポイント（https://…）")
    p.add_argument("--opensearch-index", default=OPENSEARCH_INDEX, help="opensearch: インデックス名")
    p.add_argument("--prometheus-url", default="", help="prometheus: remote write の URL（…/api/v1/remote_write）")
    p.add_argument("--anomaly-table", default="", help="detect: 異常を書く DynamoDB のテーブル名（terraform/pipeline/stream の anomalies。空なら検知しない）")
    p.add_argument("--device-map", default="", help="detect: IP や別名から機器名を引く表（別名=機器名,... 。ops/up.sh が lab/lab_topology.py --device-map で作る。sysName タグがあればそちら）")
    p.add_argument("--event-bus", default="default", help="detect: 新しい異常を put_events する EventBridge のバス名")
    # バスは既定の 1 本を共有するので、Source を接頭辞ごとに変えないと、1 つの AWS アカウントを何人かで使ったとき
    # 他の人の異常が自分のルール（terraform/workflow と terraform/pipeline/graph）に当たる。terraform が <接頭辞>.spark を渡す
    p.add_argument("--event-source", default=EVENT_SOURCE, help="detect: put_events の Source（既定 netops.spark。terraform は <接頭辞>.spark を渡す）")
    args = p.parse_args(argv)
    args.sinks = [s.strip() for s in args.sinks.split(",") if s.strip()]
    bad = [s for s in args.sinks if s not in SINKS]
    if bad or not args.sinks:
        p.error(f"--sinks は {', '.join(SINKS)} のどれか（カンマ区切り）: {args.sinks}")
    need = {"iceberg": ["iceberg_table"], "opensearch": ["opensearch_endpoint"], "prometheus": ["prometheus_url"]}
    for s in args.sinks:
        for k in need[s]:
            if not getattr(args, k):
                p.error(f"--sinks に {s} があるので --{k.replace('_', '-')} が要る")
    if not args.checkpoint.endswith("/"):
        args.checkpoint += "/"
    args.device_map = parse_device_map(args.device_map)
    for k in ("metric_topics", "log_topics"):
        setattr(args, k, ",".join(t.strip() for t in getattr(args, k).split(",") if t.strip()))
        if not getattr(args, k):
            p.error(f"--{k.replace('_', '-')} が空")
    return args


def sink_topics(sink, metric_topics, log_topics):
    """格納先が購読する Kafka のトピック（カンマ区切り）。iceberg と detect は全部、prometheus はメトリクス、opensearch はログ"""
    if sink in ("iceberg", "detect"):
        return ",".join(dict.fromkeys(metric_topics.split(",") + log_topics.split(",")))
    if sink == "prometheus":
        return metric_topics
    if sink == "opensearch":
        return log_topics
    raise ValueError(sink)


# ---------------------------------------------------------------- 行の形（Kafka → 列）
def read_rows(spark, bootstrap, topics):
    """Kafka の topics（カンマ区切り）を読んで tables.tf の列にした DataFrame を返す（テストでは start せずに中身だけ見る）"""
    from pyspark.sql import functions as F
    from pyspark.sql import types as T

    # Telegraf の JSON のうち、列に分ける部分だけ型を書く。tags / fields は文字列のまま
    schema = T.StructType([
        T.StructField("timestamp", T.LongType()),
        T.StructField("name", T.StringType()),
        T.StructField("tags", T.MapType(T.StringType(), T.StringType())),
        T.StructField("fields", T.MapType(T.StringType(), T.StringType())),
    ])
    raw = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("subscribe", topics)
        .option("startingOffsets", "earliest")
        # MSK の IAM 認証（aws-msk-iam-auth の -all jar。AWS ドキュメント「Connect to MSK with IAM」の 4 項目）
        .option("kafka.security.protocol", "SASL_SSL")
        .option("kafka.sasl.mechanism", "AWS_MSK_IAM")
        .option("kafka.sasl.jaas.config", "software.amazon.msk.auth.iam.IAMLoginModule required;")
        .option("kafka.sasl.client.callback.handler.class", "software.amazon.msk.auth.iam.IAMClientCallbackHandler")
        .load()
    )
    parsed = raw.select(
        F.col("topic"),
        F.from_json(F.col("value").cast("string"), schema).alias("m"),
    )
    rows = parsed.select(
        F.to_timestamp(F.from_unixtime(F.col("m.timestamp"))).alias("ts"),
        F.col("topic"),
        F.col("m.name").alias("measurement"),
        F.col("m.tags")["agent_host"].alias("agent_host"),
        F.col("m.tags")["host"].alias("host"),
        F.to_json(F.col("m.tags")).alias("tags_json"),
        F.to_json(F.col("m.fields")).alias("fields_json"),
        F.current_timestamp().alias("ingested_at"),
    ).where(F.col("ts").isNotNull())
    return rows


def row_to_record(row):
    """Spark の Row（tables.tf の列）を、HTTP の格納先が使う辞書にする。ts は epoch 秒（float）"""
    d = row.asDict() if hasattr(row, "asDict") else dict(row)
    ts = d.get("ts")
    if isinstance(ts, dt.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        epoch = ts.timestamp()
    else:
        epoch = float(ts)
    return {
        "ts": epoch,
        "topic": d.get("topic"),
        "measurement": d.get("measurement"),
        "agent_host": d.get("agent_host"),
        "host": d.get("host"),
        "tags": _loads(d.get("tags_json")),
        "fields": _loads(d.get("fields_json")),
    }


def _loads(s):
    if not s:
        return {}
    try:
        v = json.loads(s)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _number(v):
    """field の値を数値にする。数値でなければ None（文字列の field は Prometheus に入れない）"""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- HTTP（共通）
def http_post(url, body, headers):
    """POST して (status, body) を返す。5xx と接続エラーは HTTP_RETRIES 回まで打ち直す。4xx はそのまま返す（呼ぶ側が捨てる）"""
    last = None
    for attempt in range(1, HTTP_RETRIES + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            text = e.read()
            if e.code < 500:
                return e.code, text
            last = f"{e.code} {text[:200]!r}"
        except (urllib.error.URLError, OSError) as e:
            last = repr(e)
        time.sleep(2 * attempt)
    raise RuntimeError(f"POST {url} が {HTTP_RETRIES} 回とも失敗した: {last}")


def sigv4_headers(method, url, body, service, region, headers):
    """botocore で SigV4 の署名ヘッダーを足して返す（実行ロールの認証情報。AOSS は x-amz-content-sha256 が要る）"""
    import botocore.session
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    creds = botocore.session.get_session().get_credentials()
    if creds is None:
        raise RuntimeError("AWS の認証情報が無い（EMR Serverless の実行ロール）")
    h = dict(headers)
    h["x-amz-content-sha256"] = hashlib.sha256(body).hexdigest()
    req = AWSRequest(method=method, url=url, data=body, headers=h)
    SigV4Auth(creds.get_frozen_credentials(), service, region).add_auth(req)
    return dict(req.headers.items())


def log(msg):
    sys.stderr.write(f"[snmp_sinks] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- opensearch（_bulk）
def opensearch_docs(records):
    """_bulk の本文（action 行と document 行の対）。fields の値は数値なら数値にする（TIMESERIES 型はドキュメント ID を付けない）"""
    lines = []
    for r in records:
        doc = {
            "@timestamp": dt.datetime.fromtimestamp(r["ts"], dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "topic": r["topic"],
            "measurement": r["measurement"],
            "agent_host": r.get("agent_host"),
            "host": r.get("host"),
            "tags": r["tags"],
            "fields": {k: (_number(v) if _number(v) is not None else v) for k, v in r["fields"].items()},
        }
        lines.append('{"index":{}}')
        lines.append(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))
    return lines


def make_opensearch_sender(endpoint, index, region):
    url = endpoint.rstrip("/") + f"/{index}/_bulk"

    def send(records):
        lines = opensearch_docs(records)
        for i in range(0, len(lines), BULK_SIZE * 2):
            body = ("\n".join(lines[i:i + BULK_SIZE * 2]) + "\n").encode("utf-8")
            headers = sigv4_headers("POST", url, body, "aoss", region, {"Content-Type": "application/x-ndjson"})
            status, text = http_post(url, body, headers)
            if status >= 400:
                log(f"opensearch: _bulk が {status} を返した。{len(lines[i:i + BULK_SIZE * 2]) // 2} 件を捨てる: {text[:200]!r}")
                continue
            try:
                res = json.loads(text)
            except ValueError:
                res = {}
            if res.get("errors"):
                failed = [it["index"] for it in res.get("items", []) if it.get("index", {}).get("error")]
                log(f"opensearch: {len(failed)} 件が入らなかった（最初の 1 件: {json.dumps(failed[0], ensure_ascii=False)[:200] if failed else ''}）")
    return send


# ---------------------------------------------------------------- prometheus（remote write）
_LABEL_BAD = re.compile(r"[^a-zA-Z0-9_]")
_METRIC_BAD = re.compile(r"[^a-zA-Z0-9_:]")


def metric_name(measurement, field):
    """snmp_<measurement>_<field> を Prometheus の名前の規則（[a-zA-Z_:][a-zA-Z0-9_:]*）に収める"""
    name = _METRIC_BAD.sub("_", f"{METRIC_PREFIX}_{measurement}_{field}")
    if name[0].isdigit():
        name = "_" + name
    return name


def label_name(tag):
    name = _LABEL_BAD.sub("_", tag)
    if not name or name[0].isdigit():
        name = "_" + name
    if name.startswith("__"):
        name = "_" + name.lstrip("_")  # __ で始まる名前は予約
    return name


def prometheus_series(records):
    """数値の field を 1 系列 1 サンプルにする。[(labels(sorted list of (name, value)), value, ms), …]
    トピックでは絞らない（prometheus のクエリは --metric-topics だけを購読している）"""
    out = []
    for r in records:
        base = {}
        for k, v in r["tags"].items():
            if v is None or v == "":
                continue
            base[label_name(k)] = str(v)
        ms = int(round(r["ts"] * 1000))
        for f, v in r["fields"].items():
            num = _number(v)
            if num is None:
                continue
            labels = dict(base)
            labels["__name__"] = metric_name(r["measurement"] or "unknown", f)
            out.append((sorted(labels.items()), num, ms))
    return out


# protobuf の手組み（prometheus.WriteRequest。フィールド番号は prometheus/prompb/remote.proto と types.proto）
#   WriteRequest { repeated TimeSeries timeseries = 1; }
#   TimeSeries   { repeated Label labels = 1; repeated Sample samples = 2; }
#   Label        { string name = 1; string value = 2; }
#   Sample       { double value = 1; int64 timestamp = 2; }
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_bytes(num, payload):   # wire type 2（長さ付き）
    return _varint((num << 3) | 2) + _varint(len(payload)) + payload


def _field_varint(num, value):    # wire type 0
    if value < 0:
        value += 1 << 64
    return _varint(num << 3) + _varint(value)


def _field_double(num, value):    # wire type 1
    return _varint((num << 3) | 1) + struct.pack("<d", value)


def encode_write_request(series):
    """prometheus_series() の出力を WriteRequest の protobuf にする"""
    body = bytearray()
    for labels, value, ms in series:
        ts = bytearray()
        for name, val in labels:
            ts += _field_bytes(1, _field_bytes(1, name.encode("utf-8")) + _field_bytes(2, val.encode("utf-8")))
        ts += _field_bytes(2, _field_double(1, value) + _field_varint(2, ms))
        body += _field_bytes(1, bytes(ts))
    return bytes(body)


def snappy_compress(data, chunk=65536):
    """snappy の raw（block）形式。全部リテラルで出す（圧縮はしないが規格上正しい snappy。受け側は普通に伸長できる）。
    前置きは非圧縮長の varint、要素はタグ（下位 2 ビット 00 = リテラル。長さ - 1 が 60 以上なら 61 → 2 バイトの長さが続く）"""
    out = bytearray(_varint(len(data)))
    for i in range(0, len(data), chunk):
        part = data[i:i + chunk]
        n = len(part) - 1
        if n < 60:
            out.append(n << 2)
        else:
            out.append(61 << 2)
            out += struct.pack("<H", n)
        out += part
    return bytes(out)


def make_prometheus_sender(url, region):
    headers = {
        "Content-Type": "application/x-protobuf",
        "Content-Encoding": "snappy",
        "X-Prometheus-Remote-Write-Version": "0.1.0",
    }

    def send(records):
        series = prometheus_series(records)
        for i in range(0, len(series), BULK_SIZE):
            body = snappy_compress(encode_write_request(series[i:i + BULK_SIZE]))
            signed = sigv4_headers("POST", url, body, "aps", region, headers)
            status, text = http_post(url, body, signed)
            if status >= 400:
                # 400 は out-of-order か古すぎるサンプル（startingOffsets=earliest で最初に流れる古い分など）。打ち直しても通らないので捨てる
                log(f"prometheus: remote write が {status} を返した。{len(series[i:i + BULK_SIZE])} サンプルを捨てる: {text[:200]!r}")
    return send


# ---------------------------------------------------------------- detect（異常 → DynamoDB + EventBridge）
LINK_DOWN, LINK_UP = ".1.3.6.1.6.3.1.1.5.3", ".1.3.6.1.6.3.1.1.5.4"   # IF-MIB linkDown / linkUp の trap OID
# snmpd が起きた・止まった知らせで、異常ではない（lab の up や snmpd の再起動のたびに来る）。異常にしない。
# 知らない trap は異常として開く（許可リストにすると、知らない本物の異常を黙って捨てる）ので、ここは「捨てる」側の一覧
IGNORED_TRAPS = {
    ".1.3.6.1.6.3.1.1.5.1",       # SNMPv2-MIB coldStart
    ".1.3.6.1.6.3.1.1.5.2",       # SNMPv2-MIB warmStart
    ".1.3.6.1.4.1.8072.4.0.2",    # NET-SNMP-AGENT-MIB nsNotifyShutdown（snmpd が止まる）
    ".1.3.6.1.4.1.8072.4.0.3",    # NET-SNMP-AGENT-MIB nsNotifyRestart（snmpd が設定を読み直した）
}
# link 以外の trap には「直った」の知らせが無い。最後の trap から TRAP_TTL 秒たったら resolved にする（見回りは TRAP_SWEEP 秒に 1 回）。
# 以前は一度開くと閉じず、graph の status Lambda が機器を ALARM のままにしていた
TRAP_TTL = 600
TRAP_SWEEP = 60
EVENT_RETRIES = 3   # put_events で落ちた entry（FailedEntryCount）だけ打ち直す回数。届かなかったものは notified=false のまま次のバッチで出し直す
ANOMALY_INDEX = "status-last_seen-index"   # terraform/pipeline/stream の anomalies の GSI（status + last_seen）
EVENT_SOURCE = "netops.spark"          # --event-source の既定。terraform は接頭辞に合わせて <接頭辞>.spark を渡す
EVENT_DETAIL_TYPE = "AnomalyOpened"
EVENT_RESOLVED_TYPE = "AnomalyResolved"   # open → resolved にした瞬間に出す（terraform/pipeline/graph の status Lambda が回線を UP に戻す）


def parse_device_map(text):
    """"203.0.113.11=hq-ce-01,hq-ce-01.example.net=hq-ce-01" → {別名（小文字）: 機器名}。= の無い要素は捨てる。
    ops/up.sh が lab/lab_topology.py --device-map（lab の定義の全機器の管理 IP・全インタフェースと lo のアドレス・hostname）から作って渡す"""
    out = {}
    for p in (text or "").split(","):
        k, sep, v = p.partition("=")
        if sep and k.strip() and v.strip():
            out[k.strip().lower()] = v.strip()
    return out


IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def device(m, devmap):
    """機器名。sysName タグ（小文字にして device map を引き、無ければドメインを落とす）> device map（agent_host か source の IP）> IP そのもの > "?"。
    sysName が FQDN や大文字でもトポロジの device_id（小文字の短い名前）に合わせる"""
    t = m.get("tags") or {}
    name = str(t.get("sysName") or "").strip().lower()
    if name:
        short = name if IPV4_RE.match(name) else name.split(".", 1)[0]
        return devmap.get(name) or devmap.get(short) or short
    ip = str(t.get("agent_host") or t.get("source") or "").strip().lower()
    return devmap.get(ip, ip or "?")


def _oid(v):
    """数値 OID を ".1.3.6..." の形に揃える（Telegraf は MIB が無いと "iso.3.6..." と書く。下の varbind と同じ事情）"""
    v = str(v or "").strip()
    if v.startswith("iso."):
        return ".1." + v[4:]
    if v[:2] == "1.":
        return "." + v
    return v


def events(m, devmap):
    """1 メトリクス（Telegraf の JSON）から (機器, 種別, インタフェース, 開く/閉じる, 元) の列を出す。関係ない行は []"""
    if not isinstance(m, dict):
        return []
    name, f, t = m.get("name"), m.get("fields") or {}, m.get("tags") or {}
    if name == "interface" and "ifOperStatus" in f:
        ifn = str(t.get("ifDescr") or t.get("ifIndex") or "?")
        if ifn.startswith("lo"):
            return []
        try:
            down = int(float(f["ifOperStatus"])) == 2
        except (TypeError, ValueError):
            return []
        return [(device(m, devmap), "link_down", ifn, down, "poll")]
    if name == "snmp_trap":
        oid = _oid(t.get("oid", ""))
        if oid in (LINK_DOWN, LINK_UP):
            # MIB が無いと varbind の名前は数値 OID（末尾に ifIndex が付く）。ifDescr を優先し、無ければ ifIndex。
            # lab の Telegraf 1.40 は数値 OID を "iso.3.6.1.2.1.2.2.1.2.38" と書く（先頭が ".1." でなく "iso."。2026-09-18 実機）ので
            # 先頭を揃えてから見る。揃えないと target が "?" になり、ポーリングの eth1 と別の異常として二重に開いていた
            fields = {(".1." + k[4:] if k.startswith("iso.") else k): v for k, v in f.items()}
            ifn = "?"
            for pre in ("ifDescr", ".1.3.6.1.2.1.2.2.1.2", "ifIndex", ".1.3.6.1.2.1.2.2.1.1"):
                v = [v for k, v in fields.items() if k == pre or k.startswith(pre + ".")]
                if v:
                    ifn = str(v[0])
                    break
            return [(device(m, devmap), "link_down", ifn, oid == LINK_DOWN, "trap")]
        if oid in IGNORED_TRAPS:
            return []
        return [(device(m, devmap), "trap", oid, True, "trap")]
    return []


def anomaly_key(dev, kind, ifn):
    return f"{dev}#{kind}#{ifn}"


def anomaly_detail(kind, ifn, src):
    return f"{ifn} is down ({src})" if kind == "link_down" else f"trap {ifn}"


def _s(item, k):
    return (item.get(k) or {}).get("S", "")


def _n(item, k):
    v = (item.get(k) or {}).get("N")
    return int(v) if v is not None else 0


def make_detect_sender(table_name, devmap, region, event_bus, event_source=EVENT_SOURCE, dynamodb=None, events_client=None,
                       trap_ttl=TRAP_TTL, clock=time.time, sleep=time.sleep):
    """records（row_to_record の辞書）から異常を出し、DynamoDB に open / resolved を書き、新しく open になったものを AnomalyOpened、
    open から resolved になったものを AnomalyResolved として EventBridge に出す。戻り値は新しく open になったものだけ。

    dynamodb / events_client はテストで差し替える（無ければ boto3 で作る。EMR Serverless の Python に boto3 は入っている）。
    同じマイクロバッチに同じキーが何度も出るときは、ts の順に並べて最後の状態だけ書く（ポーリングは 10 秒間隔、トリガーは 60 秒。
    collect の順は Kafka のパーティションの順で、時刻の順ではない。ts で並べないと down → up の up が先に来たとき open のまま残る）。

    DynamoDB を先に書き、イベントはそのあとに出す。イベントが届いたかは項目の notified（BOOL）に残す:
      開く / 閉じるときに notified=false を書き、put_events が通ったら true にする。通らなかった（例外・FailedEntryCount）ものは
      false のまま残り、次のバッチで同じキーが来たとき出し直す（down / up のポーリングは 10 秒ごとに来る）。
      notified の無い古い項目は届いたものとみなす（出し直さない）。
    """
    if dynamodb is None or events_client is None:
        import boto3
        dynamodb = dynamodb or boto3.client("dynamodb", region_name=region)
        events_client = events_client or boto3.client("events", region_name=region)
    CCF = dynamodb.exceptions.ConditionalCheckFailedException
    swept = {"at": 0}

    def opened_detail(item):
        return {"anomaly_id": _s(item, "anomaly_id"), "device_id": _s(item, "device_id"), "kind": _s(item, "kind"), "target": _s(item, "target"),
                "first_seen": _n(item, "first_seen"), "detail": _s(item, "detail"), "source": _s(item, "source")}

    def resolved_detail(item, src):
        return {"anomaly_id": _s(item, "anomaly_id"), "device_id": _s(item, "device_id"), "kind": _s(item, "kind"), "target": _s(item, "target"),
                "resolved_at": _n(item, "resolved_at"), "source": src}

    def open_(key, dev, kind, ifn, src, now):
        """(新しく開いたか, 出すイベントの detail か None)"""
        values = {":o": {"S": "open"}, ":n": {"N": str(now)}, ":p": {"S": src}, ":t": {"S": anomaly_detail(kind, ifn, src)}}
        try:
            # たいていは開いたままの down（10 秒ごとのポーリング）。last_seen だけ進める
            r = dynamodb.update_item(
                TableName=table_name, Key={"anomaly_id": {"S": key}},
                ConditionExpression="#s = :o",
                UpdateExpression="SET last_seen=:n, #src=:p, detail=:t",
                ExpressionAttributeNames={"#s": "status", "#src": "source"},
                ExpressionAttributeValues=values, ReturnValues="ALL_NEW")
            item = r.get("Attributes") or {}
            if (item.get("notified") or {}).get("BOOL") is False:
                return False, opened_detail(item)   # 前のバッチで AnomalyOpened が届かなかった。出し直す
            return False, None
        except CCF:
            pass
        # 無かった、または resolved から開き直した。1 回の条件付き更新で開く: first_seen を今にし、前の resolved_at を消す。
        # workflow/worker.py は anomaly_id + first_seen を 1 つの発生として扱うので、開き直しは別の発生になる（2026-09-18）。
        # 以前は SET のあとに REMOVE を別の update_item で打っていて、あいだで落ちると first_seen が古いまま残った
        dynamodb.update_item(
            TableName=table_name, Key={"anomaly_id": {"S": key}},
            ConditionExpression="attribute_not_exists(anomaly_id) OR #s <> :o",
            UpdateExpression="SET device_id=:d, kind=:k, target=:i, #s=:o, last_seen=:n, first_seen=:n, #src=:p, detail=:t, notified=:f REMOVE resolved_at",
            ExpressionAttributeNames={"#s": "status", "#src": "source"},
            ExpressionAttributeValues={**values, ":d": {"S": dev}, ":k": {"S": kind}, ":i": {"S": ifn}, ":f": {"BOOL": False}})
        return True, {"anomaly_id": key, "device_id": dev, "kind": kind, "target": ifn, "first_seen": now,
                      "detail": anomaly_detail(kind, ifn, src), "source": src}

    def resolve(key, now):
        """出す AnomalyResolved の detail か None"""
        try:
            r = dynamodb.update_item(
                TableName=table_name, Key={"anomaly_id": {"S": key}},
                ConditionExpression="#s = :o",
                UpdateExpression="SET #s=:r, resolved_at=:n, last_seen=:n, notified=:f",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":o": {"S": "open"}, ":r": {"S": "resolved"}, ":n": {"N": str(now)}, ":f": {"BOOL": False}},
                ReturnValues="ALL_NEW", ReturnValuesOnConditionCheckFailure="ALL_OLD")
            return r.get("Attributes") or {}
        except CCF as e:
            # 開いていない異常の up（正常時のポーリングは毎回ここ）。前のバッチで AnomalyResolved が届かなかったものだけ出し直す
            old = (getattr(e, "response", None) or {}).get("Item") or {}
            if _s(old, "status") == "resolved" and (old.get("notified") or {}).get("BOOL") is False:
                return old
            return None

    def sweep_traps(now):
        """link 以外の trap で、最後の trap から trap_ttl 秒たった open を resolved にする。出す AnomalyResolved の detail の列"""
        cut = now - trap_ttl
        out, start = [], None
        while True:
            kw = {"ExclusiveStartKey": start} if start else {}
            r = dynamodb.query(
                TableName=table_name, IndexName=ANOMALY_INDEX,
                KeyConditionExpression="#s = :o AND last_seen < :c", FilterExpression="kind = :k",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":o": {"S": "open"}, ":c": {"N": str(cut)}, ":k": {"S": "trap"}}, **kw)
            for it in r.get("Items", []):
                try:
                    # GSI は結果整合なので、本体の last_seen でもう一度確かめてから閉じる（そのあいだに trap が来ていれば閉じない）
                    a = dynamodb.update_item(
                        TableName=table_name, Key={"anomaly_id": it["anomaly_id"]},
                        ConditionExpression="#s = :o AND last_seen < :c",
                        UpdateExpression="SET #s=:r, resolved_at=:n, notified=:f",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":o": {"S": "open"}, ":c": {"N": str(cut)}, ":r": {"S": "resolved"},
                                                   ":n": {"N": str(now)}, ":f": {"BOOL": False}},
                        ReturnValues="ALL_NEW")
                except CCF:
                    continue
                out.append(resolved_detail(a.get("Attributes") or {}, "ttl"))
            start = r.get("LastEvaluatedKey")
            if not start:
                return out

    def mark(detail_type, d):
        """届いたイベントの項目に notified=true を付ける。そのあいだに開き直し・閉じ直しがあれば付けない（新しい方は新しい方で出す）"""
        if detail_type == EVENT_DETAIL_TYPE:
            cond, values = "#s = :s AND first_seen = :t", {":s": {"S": "open"}, ":t": {"N": str(d["first_seen"])}}
        else:
            cond, values = "#s = :s AND resolved_at = :t", {":s": {"S": "resolved"}, ":t": {"N": str(d["resolved_at"])}}
        try:
            dynamodb.update_item(
                TableName=table_name, Key={"anomaly_id": {"S": d["anomaly_id"]}},
                ConditionExpression=cond, UpdateExpression="SET notified=:y",
                ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={**values, ":y": {"BOOL": True}})
        except CCF:
            pass

    def emit(pending):
        """[(DetailType, detail)] を put_events する（1 回 10 件まで）。落ちた entry だけ EVENT_RETRIES 回まで打ち直し、届いたものに印を付ける"""
        left = pending
        for attempt in range(1, EVENT_RETRIES + 1):
            failed = []
            for i in range(0, len(left), 10):
                chunk = left[i:i + 10]
                try:
                    r = events_client.put_events(Entries=[{"Source": event_source, "DetailType": t, "EventBusName": event_bus, "Detail": json.dumps(d)}
                                                          for t, d in chunk])
                except Exception as e:  # noqa: BLE001 - 届かない（エンドポイント・スロットリング）ときも detect のクエリを落とさず、全部を失敗として打ち直す
                    log(f"detect: put_events が例外: {type(e).__name__}: {str(e)[:200]}")
                    failed += chunk
                    continue
                results = r.get("Entries") or []
                for j, (t, d) in enumerate(chunk):
                    res = results[j] if j < len(results) else {}
                    if r.get("FailedEntryCount") and (res.get("ErrorCode") or not results):
                        failed.append((t, d))
                    else:
                        mark(t, d)
            if not failed:
                return
            log(f"detect: put_events で {len(failed)} 件失敗（{attempt} 回目）: {[d['anomaly_id'] for _, d in failed]}")
            left = failed
            if attempt < EVENT_RETRIES:
                sleep(2 * attempt)
        log(f"detect: {len(left)} 件のイベントが届かなかった（notified=false のまま。次のバッチで同じキーが来たら出し直す）")

    def send(records):
        now = int(clock())
        latest = {}
        rows = sorted((r for r in records if isinstance(r, dict)), key=lambda r: _number(r.get("ts")) or 0.0)
        for rec in rows:
            m = {"name": rec.get("measurement"), "tags": rec.get("tags") or {}, "fields": rec.get("fields") or {}}
            for dev, kind, ifn, opened, src in events(m, devmap):
                latest[anomaly_key(dev, kind, ifn)] = (dev, kind, ifn, opened, src)
        opened_now, pending = [], []
        for key, (dev, kind, ifn, opened, src) in latest.items():
            if opened:
                fresh, d = open_(key, dev, kind, ifn, src, now)
                if fresh:
                    opened_now.append(d)
                if d:
                    pending.append((EVENT_DETAIL_TYPE, d))
            else:
                item = resolve(key, now)
                if item is not None:
                    pending.append((EVENT_RESOLVED_TYPE, resolved_detail(item, src) if _s(item, "anomaly_id") else
                                    {"anomaly_id": key, "device_id": dev, "kind": kind, "target": ifn, "resolved_at": now, "source": src}))
        if now - swept["at"] >= TRAP_SWEEP:
            swept["at"] = now
            pending += [(EVENT_RESOLVED_TYPE, d) for d in sweep_traps(now)]
        if pending:
            emit(pending)
        if opened_now:
            log("detect: 新しい異常 " + ", ".join(o["anomaly_id"] for o in opened_now))
        resolved = [d["anomaly_id"] for t, d in pending if t == EVENT_RESOLVED_TYPE]
        if resolved:
            log("detect: 解消 " + ", ".join(resolved))
        return opened_now

    return send


# ---------------------------------------------------------------- クエリの組み立て
def http_query(rows, name, checkpoint, sender):
    """マイクロバッチごとに driver で collect して sender に渡す foreachBatch のクエリ"""
    def each_batch(batch_df, batch_id):
        records = [row_to_record(r) for r in batch_df.collect()]
        # detect は空のバッチでも呼ぶ（trap の TTL の見回りと、出し損ねたイベントの出し直しを送信の有無に縛らない）
        if records or name == "detect":
            sender(records)
        if records:
            log(f"{name}: batch {batch_id} で {len(records)} 行を送った")

    return (
        rows.writeStream.queryName(name)
        .foreachBatch(each_batch)
        .option("checkpointLocation", checkpoint + name + "/")
        .trigger(processingTime=TRIGGER)
        .start()
    )


def iceberg_query(rows, table, checkpoint):
    return (
        rows.writeStream.queryName("iceberg").format("iceberg")
        .outputMode("append")
        .option("checkpointLocation", checkpoint + "iceberg/")
        # テーブルの ts / topic は required だが、Spark の列は nullable のまま届く（to_timestamp と Kafka の topic）。
        # 検査を切らないと「ts should be required, but is optional」でクエリが止まる（2026-09-18 に実機で確認）。
        # ts が null の行は parse の where で落としてあり、Kafka の topic は null にならない
        .option("check-nullability", "false")
        .trigger(processingTime=TRIGGER)
        .toTable(table)
    )


def build(spark, args):
    """引数の格納先ぶんのストリーミングクエリを起こして返す"""
    queries = []
    for s in args.sinks:
        rows = read_rows(spark, args.bootstrap, sink_topics(s, args.metric_topics, args.log_topics))
        if s == "iceberg":
            queries.append(iceberg_query(rows, args.iceberg_table, args.checkpoint))
        elif s == "opensearch":
            queries.append(http_query(rows, s, args.checkpoint, make_opensearch_sender(args.opensearch_endpoint, args.opensearch_index, args.region)))
        elif s == "prometheus":
            queries.append(http_query(rows, s, args.checkpoint, make_prometheus_sender(args.prometheus_url, args.region)))
    if args.anomaly_table:
        rows = read_rows(spark, args.bootstrap, sink_topics("detect", args.metric_topics, args.log_topics))
        queries.append(http_query(rows, "detect", args.checkpoint, make_detect_sender(args.anomaly_table, args.device_map, args.region, args.event_bus, args.event_source)))
    return queries


def main(argv):
    args = parse_args(argv[1:])
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.appName("snmp_sinks").getOrCreate()
    queries = build(spark, args)
    log("格納先: " + ", ".join(f"{s}({sink_topics(s, args.metric_topics, args.log_topics)})" for s in args.sinks)
        + (f"; 検知: DynamoDB {args.anomaly_table} → EventBridge {args.event_bus}（Source {args.event_source}）" if args.anomaly_table else "; 検知: なし（--anomaly-table が空）"))
    # どれか 1 つでもクエリが止まったら、残りも止めて 1 で終わる。EMR Serverless の STREAMING モードがジョブごと起こし直し、
    # 止まったクエリも checkpoint の続きから読み直す（データは落ちない）。以前は他が動いているあいだ ERROR を出すだけでジョブが RUNNING のまま残り、
    # 一時的な失敗（HTTP の 5xx が HTTP_RETRIES 回続いた、DynamoDB / EventBridge の例外）で止まったクエリが二度と戻らなかった。
    # 起こし直しの回数はジョブの retry policy（STREAMING の既定は 1 時間に 5 回）まで。超えるとジョブが FAILED になる（docs/pipeline.md）
    dead = {}
    while not dead:
        try:
            spark.streams.awaitAnyTermination()
        except Exception:  # noqa: BLE001 - 落ちたクエリの例外。下で名前ごとに拾う
            pass
        spark.streams.resetTerminated()
        dead = {q.name: str(q.exception() or "例外なしで終了")[:500] for q in queries if not q.isActive}
    for name, why in dead.items():
        log(f"ERROR sink {name} が止まった（ジョブを 1 で終わらせて起こし直させる。checkpoint の続きから読む）: {why}")
    for q in queries:
        if q.isActive:
            try:
                q.stop()
            except Exception as e:  # noqa: BLE001 - 止めるときの例外は終わり方を変えない
                log(f"sink {q.name} を止めるときの例外: {str(e)[:200]}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
