"""異常一覧（DynamoDB。terraform/analytics の Spark ジョブ（spark/snmp_sinks.py の detect）が書く。2026-09-17 までは stream の detector Lambda）をエージェントのツールと画面に出す。

テーブル名は環境変数 ANOMALY_TABLE、無ければ SSM の <PARAM_PREFIX>/anomaly-table（terraform/stream が書く）。
どちらも無ければ「まだ配備されていない」を返して、フェーズ 1 のままの構成でも落ちない。
項目: anomaly_id（<機器>#<種別>#<対象>）, device_id, kind（link_down / trap）, target, status（open / resolved）,
first_seen / last_seen / resolved_at（epoch 秒）, source（poll / trap）, detail。
"""

import os
import time
from datetime import datetime, timezone, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError

PARAM_PREFIX = os.environ.get("PARAM_PREFIX", "")
REGION = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION") or None
INDEX = "status-last_seen-index"
TTL = 60  # SSM を引き直す間隔（秒）。無いときに毎回叩かないため
JST = timezone(timedelta(hours=9))
_cache = {"table": "", "checked": 0.0}


def table_name() -> str:
    env = os.environ.get("ANOMALY_TABLE", "")
    if env:
        return env
    if _cache["table"] or time.time() - _cache["checked"] < TTL or not PARAM_PREFIX:
        return _cache["table"]
    _cache["checked"] = time.time()
    try:
        _cache["table"] = boto3.client("ssm", region_name=REGION).get_parameter(
            Name=f"{PARAM_PREFIX}/anomaly-table")["Parameter"]["Value"]
    except (ClientError, BotoCoreError):
        _cache["table"] = ""
    return _cache["table"]


def _plain(item: dict) -> dict:
    """DynamoDB の型付き項目（{"S": ..} / {"N": ..}）を素の値に"""
    out = {}
    for k, v in item.items():
        if "S" in v:
            out[k] = v["S"]
        elif "N" in v:
            out[k] = int(v["N"]) if v["N"].lstrip("-").isdigit() else float(v["N"])
        elif "BOOL" in v:
            out[k] = v["BOOL"]
    return out


def _iso(epoch) -> str:
    return datetime.fromtimestamp(int(epoch), JST).strftime("%Y-%m-%d %H:%M:%S") if epoch else ""


def list_anomalies(status: str = "open", limit: int = 20) -> dict:
    """status（open / resolved）の異常を新しい順に。Spark が最後に見た時刻（last_seen）で並ぶ"""
    table = table_name()
    if not table:
        return {"error": "異常一覧はまだ配備されていない（terraform/stream を apply すると使える）", "anomalies": []}
    status = status if status in ("open", "resolved") else "open"
    limit = max(1, min(int(limit), 100))
    try:
        res = boto3.client("dynamodb", region_name=REGION).query(
            TableName=table, IndexName=INDEX, KeyConditionExpression="#s = :s",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": {"S": status}},
            ScanIndexForward=False, Limit=limit)
    except (ClientError, BotoCoreError) as e:
        return {"error": f"異常一覧を読めなかった: {str(e)[:200]}", "anomalies": []}
    rows = []
    for it in res.get("Items", []):
        r = _plain(it)
        r["first_seen_jst"] = _iso(r.get("first_seen"))
        r["last_seen_jst"] = _iso(r.get("last_seen"))
        if r.get("resolved_at"):
            r["resolved_at_jst"] = _iso(r["resolved_at"])
        rows.append(r)
    return {"status": status, "count": len(rows), "anomalies": rows}


TOOL_SPECS = [
    {"toolSpec": {
        "name": "list_anomalies",
        "description": "監視で見つかった異常の一覧（機器、種別 link_down / trap、対象インタフェース、発生時刻、最後に確認した時刻、poll か trap か）。"
                       "「今の異常は」「どこが落ちている」と聞かれたら status=open で呼ぶ。過去の分は status=resolved。",
        "inputSchema": {"json": {"type": "object", "properties": {
            "status": {"type": "string", "description": "open（未解消、既定）か resolved（解消済み）"},
            "limit": {"type": "integer", "description": "件数の上限（既定 20、最大 100）"},
        }}},
    }},
]
TOOLS = {"list_anomalies": list_anomalies}


def run_tool(name: str, args: dict) -> dict:
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**{k: v for k, v in (args or {}).items() if k in fn.__code__.co_varnames})
    except (TypeError, ValueError) as e:
        return {"error": str(e)}
