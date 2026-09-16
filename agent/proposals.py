"""修復案（DynamoDB。terraform/workflow のワーカーが書く）を画面に出し、人の承認・却下を書き戻す。

テーブル名は環境変数 PROPOSAL_TABLE、無ければ SSM の <PARAM_PREFIX>/proposal-table（terraform/workflow が書く）。
どちらも無ければ「まだ配備されていない」を返して、フェーズ 1 / 2 のままの構成でも落ちない。
項目: proposal_id（= anomaly_id）, anomaly_id, device_id, kind, target, first_seen（異常の発生時刻）,
status（pending → approved / rejected（人）→ applied → verified / failed（ワーカー）、expired（時間切れ））,
cause, action（heal-main / check / none）, command, reason, agent_response, workflow_id,
created_at / updated_at / decided_at（epoch 秒）, decided_by, apply_output, verify_note。
承認・却下は status = pending のときだけ通る（ConditionExpression）。ワーカーは Temporal のシグナルではなく、
このテーブルの status をポーリングして進む（画面と Temporal を直接つながない）。
"""

import os
import time
from datetime import datetime, timezone, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError

PARAM_PREFIX = os.environ.get("PARAM_PREFIX", "")
REGION = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION") or None
INDEX = "status-updated_at-index"
STATUSES = ("pending", "approved", "rejected", "applied", "verified", "failed", "expired")
DECISIONS = ("approved", "rejected")
TTL = 60
JST = timezone(timedelta(hours=9))
_cache = {"table": "", "checked": 0.0}


def table_name() -> str:
    env = os.environ.get("PROPOSAL_TABLE", "")
    if env:
        return env
    if _cache["table"] or time.time() - _cache["checked"] < TTL or not PARAM_PREFIX:
        return _cache["table"]
    _cache["checked"] = time.time()
    try:
        _cache["table"] = boto3.client("ssm", region_name=REGION).get_parameter(
            Name=f"{PARAM_PREFIX}/proposal-table")["Parameter"]["Value"]
    except (ClientError, BotoCoreError):
        _cache["table"] = ""
    return _cache["table"]


def _plain(item: dict) -> dict:
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


def _decorate(p: dict) -> dict:
    for k in ("created_at", "updated_at", "decided_at", "first_seen"):
        p[f"{k}_jst"] = _iso(p.get(k))
    return p


def list_proposals(status: str = "pending", limit: int = 50) -> dict:
    """status の修復案を新しい順に（updated_at）。status が all なら全件（Scan）"""
    table = table_name()
    if not table:
        return {"error": "修復案はまだ配備されていない（terraform/workflow を apply すると使える）", "proposals": []}
    limit = max(1, min(int(limit), 100))
    client = boto3.client("dynamodb", region_name=REGION)
    try:
        if status == "all":
            res = client.scan(TableName=table, Limit=limit)
            items = sorted(res.get("Items", []), key=lambda i: int(i.get("updated_at", {}).get("N", "0")), reverse=True)
        else:
            status = status if status in STATUSES else "pending"
            res = client.query(
                TableName=table, IndexName=INDEX, KeyConditionExpression="#s = :s",
                ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": {"S": status}},
                ScanIndexForward=False, Limit=limit)
            items = res.get("Items", [])
    except (ClientError, BotoCoreError) as e:
        return {"error": f"修復案を読めない: {str(e)[:200]}", "proposals": []}
    proposals = [_decorate(_plain(i)) for i in items]
    return {"status": status, "count": len(proposals), "proposals": proposals}


def get_proposal(proposal_id: str) -> dict:
    table = table_name()
    if not table:
        return {}
    try:
        res = boto3.client("dynamodb", region_name=REGION).get_item(TableName=table, Key={"proposal_id": {"S": proposal_id}})
    except (ClientError, BotoCoreError):
        return {}
    return _decorate(_plain(res["Item"])) if "Item" in res else {}


def decide(proposal_id: str, decision: str, decided_by: str = "web") -> dict:
    """pending の修復案を approved / rejected にする。pending でなければ何もしない（誰かが先に決めた・ワーカーが進めた）"""
    table = table_name()
    if not table:
        return {"error": "修復案はまだ配備されていない（terraform/workflow を apply すると使える）"}
    if decision not in DECISIONS:
        return {"error": f"decision は {' / '.join(DECISIONS)} のどれか"}
    if not proposal_id:
        return {"error": "proposal_id が空"}
    now = int(time.time())
    try:
        boto3.client("dynamodb", region_name=REGION).update_item(
            TableName=table, Key={"proposal_id": {"S": proposal_id}},
            UpdateExpression="SET #s = :d, decided_by = :b, decided_at = :n, updated_at = :n",
            ConditionExpression="#s = :p",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": {"S": decision}, ":p": {"S": "pending"}, ":b": {"S": decided_by[:64]}, ":n": {"N": str(now)}})
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {"error": f"{proposal_id} は pending ではない（先に決まったか、ワーカーが進めた）"}
        return {"error": f"更新できない: {str(e)[:200]}"}
    except BotoCoreError as e:
        return {"error": f"更新できない: {str(e)[:200]}"}
    return {"proposal_id": proposal_id, "status": decision, "decided_by": decided_by, "decided_at": now}
