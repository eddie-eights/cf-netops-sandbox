"""Kafka（MSK、IAM 認証）のトピックを読み、選んだ格納先に流し続ける Spark Structured Streaming のジョブ（Kafka の 4 分岐）。

EMR Serverless の上で動く（terraform/pipeline/analytics）。起動は ops/up.sh の a-3（start-job-run）で、引数は terraform/pipeline/analytics の
output job_driver_json が組み立てる（--bootstrap / --checkpoint / --sinks と、格納先ごとの --iceberg-table などの値）。
Kafka と S3 Tables の jar、カタログの設定は spark-submit の --conf で渡す。

格納先は 4 つ（--sinks にカンマ区切り）:
  iceberg     全トピック → S3 Tables（Iceberg）のテーブルに append（履歴の正本）
  opensearch  ログのトピックだけ → OpenSearch Serverless（TIMESERIES 型のコレクション）の _bulk に SigV4 で POST
  prometheus  メトリクスのトピックだけ → Amazon Managed Service for Prometheus の remote write に SigV4 で POST（数値の field だけ）
  splunk      全トピック → Splunk の HTTP Event Collector（HEC）に 1 行 1 イベントで POST（Authorization: Splunk <token>。
              token は SSM の SecureString（--splunk-token-parameter）から起動時に読む。送り先は analytics の ECS の Splunk Enterprise
              （https://splunk.<prefix>.internal:8088、自己署名なので --splunk-skip-verify。VPC の中。2026-09-28 から AWS の外の Splunk へは送らない）。
              2026-09-26 まで MSK Connect の Splunk Connect for Kafka にする予定だったが、Spark から直接書くことにした）
どのトピックがメトリクスでどれがログかは --metric-topics / --log-topics（既定は Telegraf の metrics と traps,logs。
logs は機器の syslog。SR Linux が lab の EC2 へ送り、lab の EC2 が Telegraf（ECS）の内部 NLB へ DNAT する）。格納先ごとに別のストリーミングクエリ（別の Kafka の購読と checkpoint）に
する。1 つが止まったらジョブを 1 で終わらせ、EMR Serverless に起こし直させる（どのクエリも checkpoint の続きから読む）。

Telegraf の JSON 出力（outputs.kafka の data_format = "json"、json_timestamp_units = "1s"）は
  {"fields": {…}, "name": "<measurement>", "tags": {"agent_host": "…", "host": "…", …}, "timestamp": <秒>}
の形。列に分けるのは timestamp / name / agent_host / host だけで、tags と fields は JSON 文字列のまま入れる
（機器やメトリクスが増えてもテーブルの列を変えないため。terraform/pipeline/analytics/tables.tf の列と同じ）。

異常の検知はここではしない（2026-10-02 にやめた。detect のクエリと Neptune の anomaly 頂点、S3 Tables の anomaly_events、EventBridge への put_events を消した）。
検知と相関は格納先の側でする: Grafana のアラートルール（AMP の ifOperStatus。grafana/provisioning/alerting）と Splunk の保存済みサーチ
（trap・gNMI の BGP / IS-IS。splunk/netops_alerts）が SNS のトピック <接頭辞>-alerts に出し、ワークフロー（SQS）と
トポロジの status（graph の Lambda）がそれを受ける。

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

METRIC_TOPICS = "metrics,gnmi"   # metrics = Telegraf の inputs.snmp、gnmi = inputs.gnmi（telegraf/telegraf.conf.in。Telegraf（ECS）で動く）
LOG_TOPICS = "traps,logs"   # traps = Telegraf の inputs.snmp_trap、logs = inputs.syslog（機器の syslog。measurement は device_log）
SINKS = ("iceberg", "opensearch", "prometheus", "splunk")
TRIGGER = "60 seconds"
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3        # 5xx と接続エラーだけ打ち直す。4xx は捨ててログに出す（古すぎるサンプルなどは何度打っても通らない）
BULK_SIZE = 500         # 1 回の POST に載せる行数
OPENSEARCH_INDEX = "snmp-logs"
METRIC_PREFIX = "snmp"
SPLUNK_HEC_PATH = "/services/collector/event"   # HEC の JSON イベントの入口（--splunk-hec-url に無ければ足す）
SPLUNK_SOURCETYPE_PREFIX = "netops"             # sourcetype は netops:<トピック>（netops:metrics / netops:traps / netops:logs）


# ---------------------------------------------------------------- 引数
def parse_args(argv):
    p = argparse.ArgumentParser(prog="snmp_sinks.py", description=__doc__.split("\n")[0])
    p.add_argument("--bootstrap", required=True, help="MSK の bootstrap servers（SASL/IAM、9098）")
    p.add_argument("--checkpoint", required=True, help="checkpoint の親（s3://<バケット>/analytics/checkpoint/。格納先ごとに下にディレクトリを切る）")
    p.add_argument("--sinks", required=True, help="格納先（カンマ区切り。iceberg / opensearch / prometheus / splunk）")
    p.add_argument("--region", default="ap-northeast-1", help="SigV4 のリージョン")
    p.add_argument("--metric-topics", default=METRIC_TOPICS, help="メトリクスのトピック（カンマ区切り。iceberg と prometheus が読む）")
    p.add_argument("--log-topics", default=LOG_TOPICS, help="ログのトピック（カンマ区切り。iceberg と opensearch が読む）")
    p.add_argument("--iceberg-table", default="", help="iceberg: catalog.namespace.table")
    p.add_argument("--opensearch-endpoint", default="", help="opensearch: コレクションのエンドポイント（https://…）")
    p.add_argument("--opensearch-index", default=OPENSEARCH_INDEX, help="opensearch: インデックス名")
    p.add_argument("--prometheus-url", default="", help="prometheus: remote write の URL（…/api/v1/remote_write）")
    p.add_argument("--splunk-hec-url", default="", help="splunk: HEC の URL（https://<host>:8088。/services/collector/event が無ければ足す）")
    p.add_argument("--splunk-token-parameter", default="", help="splunk: HEC の token を入れた SSM の SecureString の名前（/<接頭辞>/splunk/hec-token。値は起動時に読み、ログに出さない）")
    p.add_argument("--splunk-index", default="", help="splunk: イベントを入れる index（空なら token の既定の index）")
    p.add_argument("--splunk-skip-verify", action="store_true", help="splunk: HEC の TLS 証明書を検証しない（自己署名の Splunk Enterprise の検証用。既定は検証する）")
    args = p.parse_args(argv)
    args.sinks = [s.strip() for s in args.sinks.split(",") if s.strip()]
    bad = [s for s in args.sinks if s not in SINKS]
    if bad or not args.sinks:
        p.error(f"--sinks は {', '.join(SINKS)} のどれか（カンマ区切り）: {args.sinks}")
    need = {"iceberg": ["iceberg_table"], "opensearch": ["opensearch_endpoint"], "prometheus": ["prometheus_url"],
            "splunk": ["splunk_hec_url", "splunk_token_parameter"]}
    for s in args.sinks:
        for k in need[s]:
            if not getattr(args, k):
                p.error(f"--sinks に {s} があるので --{k.replace('_', '-')} が要る")
    if not args.checkpoint.endswith("/"):
        args.checkpoint += "/"
    for k in ("metric_topics", "log_topics"):
        setattr(args, k, ",".join(t.strip() for t in getattr(args, k).split(",") if t.strip()))
        if not getattr(args, k):
            p.error(f"--{k.replace('_', '-')} が空")
    return args


def sink_topics(sink, metric_topics, log_topics):
    """格納先が購読する Kafka のトピック（カンマ区切り）。iceberg / splunk は全部、prometheus はメトリクス、opensearch はログ"""
    if sink in ("iceberg", "splunk"):
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
def http_post(url, body, headers, context=None):
    """POST して (status, body) を返す。5xx と接続エラーは HTTP_RETRIES 回まで打ち直す。4xx はそのまま返す（呼ぶ側が捨てる）。
    context は TLS の設定（splunk の --splunk-skip-verify だけが渡す。無ければ既定の検証）"""
    last = None
    for attempt in range(1, HTTP_RETRIES + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=context) as r:
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


# ---------------------------------------------------------------- splunk（HTTP Event Collector）
def splunk_hec_url(url):
    """--splunk-hec-url を HEC のイベントの入口に揃える。https://host:8088 → …/services/collector/event、…/services/collector → …/event"""
    u = url.rstrip("/")
    if u.endswith(SPLUNK_HEC_PATH):
        return u
    if u.endswith("/services/collector"):
        return u + "/event"
    return u + SPLUNK_HEC_PATH


def _splunk_value(v):
    """field の値。数値の文字列（Telegraf は SNMP の Counter などを文字列で出す）は数値にし、bool / 数値 / それ以外の文字列はそのまま"""
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v
    return v


def splunk_events(records, index=""):
    """HEC の JSON イベント（1 行 1 イベント。HEC は本文に並べた複数のイベントを 1 回で受ける）。
    time は epoch 秒、host は機器（無ければ Telegraf の agent_host）、sourcetype は netops:<トピック>、event に measurement / tags / fields。
    fields の数値の文字列は数値にする（Splunk が検索で数として扱えるように）"""
    lines = []
    for r in records:
        ev = {
            "time": r["ts"],
            "host": r.get("host") or r.get("agent_host") or "unknown",
            "source": f"telegraf:{r['measurement'] or 'unknown'}",
            "sourcetype": f"{SPLUNK_SOURCETYPE_PREFIX}:{r['topic']}",
            "event": {
                "topic": r["topic"],
                "measurement": r["measurement"],
                "agent_host": r.get("agent_host"),
                "tags": r["tags"],
                "fields": {k: _splunk_value(v) for k, v in r["fields"].items()},
            },
        }
        if index:
            ev["index"] = index
        lines.append(json.dumps(ev, separators=(",", ":"), ensure_ascii=False))
    return lines


def read_ssm_parameter(name, region):
    """SSM の SecureString を復号して読む（HEC の token。EMR Serverless の実行ロールに ssm:GetParameter。値はログに出さない）"""
    import boto3
    return boto3.client("ssm", region_name=region).get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def make_splunk_sender(url, token, index="", skip_verify=False):
    url = splunk_hec_url(url)
    headers = {"Authorization": f"Splunk {token}", "Content-Type": "application/json"}
    context = None
    if skip_verify:
        import ssl
        context = ssl._create_unverified_context()  # noqa: S323 - 自己署名の Splunk Enterprise の検証用。既定は検証する

    def send(records):
        lines = splunk_events(records, index)
        for i in range(0, len(lines), BULK_SIZE):
            body = "\n".join(lines[i:i + BULK_SIZE]).encode("utf-8")
            status, text = http_post(url, body, headers, context)
            if status >= 400:
                # 400 は本文の形（time や event が無い）、401 / 403 は token（無効・無効化・index の許可が無い）。打ち直しても通らないので捨てる。
                # token の値は出さない（Splunk の応答にも入っていない）
                log(f"splunk: HEC が {status} を返した。{len(lines[i:i + BULK_SIZE])} 件を捨てる: {text[:200]!r}")
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


# ---------------------------------------------------------------- クエリの組み立て
def http_query(rows, name, checkpoint, sender):
    """マイクロバッチごとに driver で collect して sender に渡す foreachBatch のクエリ"""
    def each_batch(batch_df, batch_id):
        records = [row_to_record(r) for r in batch_df.collect()]
        if records:
            sender(records)
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


KAFKA_IAM_PROPS = {"security.protocol": "SASL_SSL", "sasl.mechanism": "AWS_MSK_IAM",
                   "sasl.jaas.config": "software.amazon.msk.auth.iam.IAMLoginModule required;",
                   "sasl.client.callback.handler.class": "software.amazon.msk.auth.iam.IAMClientCallbackHandler"}


def all_topics(args):
    """引数の格納先が読むトピックの和（重複なし、引数の順）"""
    seen = []
    for s in args.sinks:
        for t in sink_topics(s, args.metric_topics, args.log_topics).split(","):
            if t and t not in seen:
                seen.append(t)
    return seen


def ensure_topics(spark, bootstrap, topics):
    """無いトピックを作って、作った名前を返す（あるものは触らない）。
    MSK は auto.create.topics.enable=true だが、それは produce のとき。Telegraf が最初の trap / syslog を出すまで traps / logs は無く
    （SNMP のポーリングを止めている（Telegraf の SNMP_POLL=0。既定）と metrics もずっと無い）、
    Spark の offset 読み（AdminClient）は無いトピックで UnknownTopicOrPartitionException で落ちて、起こし直しの上限（1 時間 5 回）を
    使い切っていた（2026-09-27 実測）。パーティション数と複製数はブローカーの既定（terraform/pipeline/stream の MSK configuration）。
    Telegraf と同時に作って TopicExistsException になっても、あるのだから先へ進む"""
    jvm = spark._jvm
    props = jvm.java.util.Properties()
    props.put("bootstrap.servers", bootstrap)
    for k, v in KAFKA_IAM_PROPS.items():
        props.put(k, v)
    admin = jvm.org.apache.kafka.clients.admin.AdminClient.create(props)
    try:
        have = set(admin.listTopics().names().get())
        missing = [t for t in topics if t not in have]
        if missing:
            none = jvm.java.util.Optional.empty()
            new = jvm.java.util.ArrayList()
            for t in missing:
                new.add(jvm.org.apache.kafka.clients.admin.NewTopic(t, none, none))
            try:
                admin.createTopics(new).all().get()
            except Exception as e:  # noqa: BLE001 - py4j の例外。TopicExists だけ許す
                if "TopicExistsException" not in str(e):
                    raise
        return missing
    finally:
        admin.close()


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
        elif s == "splunk":
            # token は起動時に 1 回だけ読む（driver の中に置く。ログにも引数にも出ない）。読めなければジョブが起動で落ち、原因が stderr に出る
            token = read_ssm_parameter(args.splunk_token_parameter, args.region)
            queries.append(http_query(rows, s, args.checkpoint, make_splunk_sender(args.splunk_hec_url, token, args.splunk_index, args.splunk_skip_verify)))
    return queries


def main(argv):
    args = parse_args(argv[1:])
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.appName("snmp_sinks").getOrCreate()
    made = ensure_topics(spark, args.bootstrap, all_topics(args))
    log("トピック: " + ", ".join(all_topics(args)) + (f"（作った: {', '.join(made)}）" if made else "（全部あった）"))
    queries = build(spark, args)
    log("格納先: " + ", ".join(f"{s}({sink_topics(s, args.metric_topics, args.log_topics)})" for s in args.sinks))
    # どれか 1 つでもクエリが止まったら、残りも止めて 1 で終わる。EMR Serverless の STREAMING モードがジョブごと起こし直し、
    # 止まったクエリも checkpoint の続きから読み直す（データは落ちない）。以前は他が動いているあいだ ERROR を出すだけでジョブが RUNNING のまま残り、
    # 一時的な失敗（HTTP の 5xx が HTTP_RETRIES 回続いた、S3 Tables の書き込みの失敗）で止まったクエリが二度と戻らなかった。
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
