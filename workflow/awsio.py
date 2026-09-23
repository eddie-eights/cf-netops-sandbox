"""ワーカーの「外に触る」部分。環境変数の読み出しと AWS 呼び出しをここにまとめる。

worker.py のアクティビティは全部このファイルの関数を asyncio.to_thread で呼ぶ。
Temporal のワークフロー（決定的でないといけない）から直接呼ぶものは 1 つも無い。

  Neptune    異常の「いま」（label anomaly。Spark の detect が書く）を読み、修復案の「いま」（label proposal）を読み書きする
  S3 Tables  修復案の証跡（proposal_events）に追記する（PyIceberg。作成・承認・却下・適用・確認を 1 行ずつ）
  AgentCore  Runtime を invoke して原因分析を答えさせる
  SSM        lab EC2 に Run Command で 1 行打つ
  SQS        Spark の検知（EventBridge → SQS）を long polling で受け取る

以前は異常も修復案も DynamoDB だった（2026-09-24 に Neptune と S3 Tables に寄せた）。

boto3 / pyiceberg / pyarrow は import せず、呼ばれたときに関数の中で読む。Temporal のワークフローサンドボックスが
このモジュールを再 import するときに重い依存を引きずらないようにするため。
"""

import datetime as dt
import json
import os
import time
import uuid

ANOMALY_QUEUE_URL = os.environ.get("ANOMALY_QUEUE_URL", "")  # terraform/workflow の events.tf。空なら Neptune の open を polling
NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")    # host:8182（terraform/pipeline/graph）
AUDIT_TABLE_BUCKET_ARN = os.environ.get("AUDIT_TABLE_BUCKET_ARN", "")  # terraform/pipeline/analytics の S3 Tables のバケット
AUDIT_NAMESPACE = os.environ.get("AUDIT_NAMESPACE", "")
PROPOSAL_EVENTS_TABLE = os.environ.get("PROPOSAL_EVENTS_TABLE", "proposal_events")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
LAB_INSTANCE_ID = os.environ.get("LAB_INSTANCE_ID", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
COMMIT_RETRIES = 5  # 証跡の append が他のアクティビティの append とぶつかった（CommitFailedException）ときに読み直して打ち直す回数

_cache = {}


def _boto(name, config=None, **kw):
    import boto3

    return boto3.client(name, region_name=REGION, config=config, **kw)


def _agent_config():
    """AgentCore の invoke は、エージェントがツールを何本も回すと 1 分を超える。botocore の既定（読み取り 60 秒・3 回まで再送）だと
    途中で切って同じ質問をもう一度投げ、アクティビティの start_to_close（worker.py）に届く前に 3 倍の時間と費用を使う。
    読み取りを 150 秒まで待ち、再送はしない（やり直しは Temporal の RetryPolicy に任せる）"""
    from botocore.config import Config

    return Config(read_timeout=150, connect_timeout=10, retries={"max_attempts": 1})


# ---------------------------------------------------------------- Neptune（異常と修復案の「いま」）
_ESCAPES = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _q(v) -> str:
    """Gremlin のリテラル（agent/graph.py の _q と同じ）。Neptune の文字列の Gremlin は生の改行や制御文字を受け付けないので
    \\n / \\uXXXX に直す（エージェントの答えの本文に何が入っても壊れない）"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + "".join(_ESCAPES.get(ch) or ("\\u%04x" % ord(ch) if ord(ch) < 0x20 or ord(ch) == 0x7F else ch) for ch in str(v)) + "'"


def _un(v):
    """GraphSON 3 の型付き値を素の Python に（agent/graph.py の _un と同じ）"""
    if isinstance(v, dict) and "@type" in v:
        t, val = v["@type"], v.get("@value")
        if t in ("g:List", "g:Set"):
            return [_un(x) for x in val]
        if t == "g:Map":
            it = iter(val)
            return {_un(k): _un(x) for k, x in zip(it, it)}
        return _un(val)
    if isinstance(v, dict):
        return {k: _un(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_un(x) for x in v]
    return v


def gremlin(q: str) -> list:
    """neptunedata で Gremlin を 1 本打ち、結果の list を返す（IAM 認証の署名は boto3 が付ける）。クライアントは使い回す"""
    if "neptune" not in _cache:
        from botocore.config import Config

        _cache["neptune"] = _boto("neptunedata", Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 2}),
                                  endpoint_url=f"https://{NEPTUNE_ENDPOINT}")
    res = _un(_cache["neptune"].execute_gremlin_query(gremlinQuery=q).get("result"))
    data = res.get("data", []) if isinstance(res, dict) else res
    return data if isinstance(data, list) else ([] if data is None else [data])


def _item(m: dict, key: str) -> dict:
    """elementMap() の 1 件を、id を key（anomaly_id / proposal_id）に置き換えた dict にする"""
    d = {k: v for k, v in m.items() if k not in ("id", "label")}
    d[key] = m.get("id")
    return d


def _props(fields: dict) -> str:
    """property(single, …) の並び。Neptune の既定は set（同じ key に値が増える）なので single を付ける。None と空文字は書かない"""
    return "".join(f".property(single,{_q(k)},{_q(v)})" for k, v in fields.items() if v is not None and v != "")


def list_open_anomalies(limit: int = 50) -> list:
    return [_item(m, "anomaly_id") for m in gremlin(
        f"g.V().hasLabel('anomaly').has('status','open').order().by('last_seen',desc).limit({int(limit)}).elementMap()")]


def read_anomaly(anomaly_id: str) -> dict:
    rows = gremlin(f"g.V({_q(anomaly_id)}).hasLabel('anomaly').elementMap()")
    return _item(rows[0], "anomaly_id") if rows else {}


def read_proposal(proposal_id: str) -> dict:
    rows = gremlin(f"g.V({_q(proposal_id)}).hasLabel('proposal').elementMap()")
    return _item(rows[0], "proposal_id") if rows else {}


def write_proposal(item: dict, only_new: bool = False) -> bool:
    """修復案の頂点を書く。only_new なら同じ proposal_id が無いときだけ作り、あれば書かずに False（人が決めた status を pending に戻さない）"""
    pid = _q(item["proposal_id"])
    props = _props({k: v for k, v in item.items() if k != "proposal_id"})
    if only_new:
        res = gremlin(f"g.V({pid}).fold().coalesce(unfold().constant(false),addV('proposal').property(id,{pid}){props}.constant(true))")
        return bool(res and res[0] is True)
    gremlin(f"g.V({pid}).fold().coalesce(unfold(),addV('proposal').property(id,{pid})){props}.id()")
    return True


def update_proposal(proposal_id: str, fields: dict, only_status: str | None = None) -> bool:
    """修復案の頂点の fields を書き換える（updated_at は今）。only_status なら status がその値のときだけ書く。書けたら True"""
    cond = f".has('status',{_q(only_status)})" if only_status else ""
    props = _props({**fields, "updated_at": int(time.time())})
    return bool(gremlin(f"g.V({_q(proposal_id)}).hasLabel('proposal'){cond}{props}.id()"))


# ---------------------------------------------------------------- S3 Tables（修復案の証跡）
def catalog_properties() -> dict:
    """S3 Tables の Iceberg REST エンドポイントにつなぐ PyIceberg の設定（SigV4 の署名名は s3tables）"""
    return {
        "type": "rest", "warehouse": AUDIT_TABLE_BUCKET_ARN, "uri": f"https://s3tables.{REGION}.amazonaws.com/iceberg",
        "rest.sigv4-enabled": "true", "rest.signing-name": "s3tables", "rest.signing-region": REGION, "s3.region": REGION,
    }


def audit_rows(rows: list, columns) -> list:
    """rules.proposal_event の行を、列の型（timestamptz は epoch 秒 → UTC の datetime）に合わせた dict にする"""
    def conv(v, t):
        if v is None:
            return None
        return dt.datetime.fromtimestamp(int(v), dt.timezone.utc) if t == "timestamptz" else str(v)
    return [{n: conv(r.get(n), t) for n, t in columns} for r in rows]


def append_proposal_events(rows: list, columns) -> None:
    """proposal_events に rows を append する。同じテーブルへの append がぶつかったら（CommitFailedException）読み直して打ち直す"""
    if not rows:
        return
    import pyarrow as pa
    from pyiceberg.exceptions import CommitFailedException

    if "catalog" not in _cache:
        from pyiceberg.catalog import load_catalog

        _cache["catalog"] = load_catalog("s3tables", **catalog_properties())
    data = audit_rows(rows, columns)
    for attempt in range(COMMIT_RETRIES):
        table = _cache["catalog"].load_table(f"{AUDIT_NAMESPACE}.{PROPOSAL_EVENTS_TABLE}")
        try:
            table.append(pa.Table.from_pylist(data, schema=table.schema().as_arrow()))
            return
        except CommitFailedException:
            if attempt == COMMIT_RETRIES - 1:
                raise
            time.sleep(1 + attempt)


# ---------------------------------------------------------------- AgentCore Runtime
def ask_agent(prompt: str) -> str:
    session_id = f"workflow-{uuid.uuid4()}"  # runtimeSessionId は 33 文字以上
    res = _boto("bedrock-agentcore", _agent_config()).invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN, runtimeSessionId=session_id, qualifier="DEFAULT",
        contentType="application/json", accept="application/json",
        payload=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"))
    data = json.loads(res["response"].read())
    if not isinstance(data, dict) or data.get("status") != "success":
        raise RuntimeError(f"agent error: {str(data)[:300]}")
    return data.get("response", "")


# ---------------------------------------------------------------- SSM（lab EC2）
def run_on_lab(command: str, timeout: int = 120) -> tuple[str, str]:
    """SSM Run Command で lab EC2 に 1 行打ち、(status, output) を返す"""
    ssm = _boto("ssm")
    cmd = ssm.send_command(
        InstanceIds=[LAB_INSTANCE_ID], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command], "executionTimeout": [str(timeout)]}, TimeoutSeconds=60)
    cid = cmd["Command"]["CommandId"]
    deadline = time.time() + timeout + 30
    while time.time() < deadline:
        time.sleep(3)
        try:
            inv = ssm.get_command_invocation(CommandId=cid, InstanceId=LAB_INSTANCE_ID)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if inv["Status"] in ("Pending", "InProgress", "Delayed"):
            continue
        out = (inv.get("StandardOutputContent", "") + inv.get("StandardErrorContent", ""))[:4000]
        return inv["Status"], out
    return "TimedOut", ""


# ---------------------------------------------------------------- SQS（Spark の検知）
def receive_messages() -> list:
    return _boto("sqs").receive_message(QueueUrl=ANOMALY_QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=20).get("Messages", [])


def delete_message(receipt: str) -> None:
    _boto("sqs").delete_message(QueueUrl=ANOMALY_QUEUE_URL, ReceiptHandle=receipt)
