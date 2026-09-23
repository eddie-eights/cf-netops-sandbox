"""修復案（DynamoDB。terraform/workflow のワーカーが書く）を画面に出し、人の承認・却下を書き戻す。

テーブル名は環境変数 PROPOSAL_TABLE、無ければ SSM の <PARAM_PREFIX>/proposal-table（terraform/workflow が書く）。
どちらも無ければ「まだ配備されていない」を返して、WORKFLOW を作っていない構成でも落ちない。
項目: proposal_id（= <anomaly_id>#<first_seen>。発生ごとに 1 件。閉じて開き直した次の発生は別の行）, anomaly_id, device_id, kind, target,
first_seen（異常の発生時刻）, status（pending → approved / rejected（人）→ applied → verified / failed（ワーカー）、expired（時間切れ）、
obsolete（承認のあいだに異常が閉じた・開き直したので打たなかった））,
cause, action（heal-main / check / none）, command, reason, agent_response, workflow_id,
created_at / updated_at / decided_at（epoch 秒）, decided_by, apply_output, verify_note。
承認・却下は status = pending のときだけ通る（ConditionExpression）。ワーカーは Temporal のシグナルではなく、
このテーブルの status をポーリングして進む（画面と Temporal を直接つながない）。
DynamoDB の読み方（クライアントの使い回し・表名・整形）は agent/toolkit.py に置いてある（anomalies.py と共通）。
"""

import time

from botocore.exceptions import BotoCoreError, ClientError

import toolkit

INDEX = "status-updated_at-index"  # GSI。パーティションキーが status、ソートキーが updated_at
STATUSES = ("pending", "approved", "rejected", "applied", "verified", "failed", "expired", "obsolete")
QUERYABLE = STATUSES + ("all",)  # 一覧で指定できる値（all は全部）
DECISIONS = ("approved", "rejected")  # 人が決められるのはこの 2 つだけ
TABLE = toolkit.Param("PROPOSAL_TABLE", "proposal-table")  # 表の名前（環境変数か SSM）


def _decorate(p: dict) -> dict:
    """epoch 秒の項目に、読める形（JST）を並べて足す"""
    for k in ("created_at", "updated_at", "decided_at", "first_seen"):
        p[f"{k}_jst"] = toolkit.jst(p.get(k))
    return p


def list_proposals(status: str = "pending", limit: int = 50, device_id: str = "") -> dict:
    """status の修復案を新しい順に（updated_at）。status が all なら全件（Scan）。device_id があればその機器だけ。

    all だけ Scan なのは、GSI のパーティションキーが status で、8 つの status を 1 回の Query では取れないため
    （8 回 Query するより 1 往復の Scan のほうが安い。この PoC の表は数十件）。
    """
    table = TABLE.value()
    if not table:
        return {"error": "修復案はまだ配備されていない（terraform/workflow を apply すると使える）", "proposals": []}
    status = status if status in QUERYABLE else "pending"
    limit = max(1, min(int(limit), 100))
    read = toolkit.read_count(device_id, limit)
    client = toolkit.client("dynamodb")
    try:
        if status == "all":
            res = client.scan(TableName=table, Limit=read)
            items = sorted(res.get("Items", []), key=lambda i: int(i.get("updated_at", {}).get("N", "0")), reverse=True)
        else:
            res = client.query(
                TableName=table, IndexName=INDEX, KeyConditionExpression="#s = :s",
                ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": {"S": status}},
                ScanIndexForward=False, Limit=read)
            items = res.get("Items", [])
    except (ClientError, BotoCoreError) as e:
        return {"error": f"修復案を読めない: {str(e)[:200]}", "proposals": []}
    # 機器で絞って limit 件に切ってから整形する（返さない行を整形しても捨てるだけなので）
    proposals = [_decorate(toolkit.plain(i)) for i in toolkit.narrow(items, device_id, limit)]
    return {"status": status, "count": len(proposals), "proposals": proposals}


def get_proposal(proposal_id: str) -> dict:
    """1 件だけ引く（画面の承認タブが、決める直前の状態を確かめるのに使う）。無ければ空の辞書"""
    table = TABLE.value()
    if not table:
        return {}
    try:
        res = toolkit.client("dynamodb").get_item(TableName=table, Key={"proposal_id": {"S": proposal_id}})
    except (ClientError, BotoCoreError):
        return {}
    return _decorate(toolkit.plain(res["Item"])) if "Item" in res else {}


def decide(proposal_id: str, decision: str, decided_by: str = "web") -> dict:
    """pending の修復案を approved / rejected にする。pending でなければ何もしない（誰かが先に決めた・ワーカーが進めた）"""
    table = TABLE.value()
    if not table:
        return {"error": "修復案はまだ配備されていない（terraform/workflow を apply すると使える）"}
    if decision not in DECISIONS:
        return {"error": f"decision は {' / '.join(DECISIONS)} のどれか"}
    if not proposal_id:
        return {"error": "proposal_id が空"}
    now = int(time.time())
    try:
        toolkit.client("dynamodb").update_item(
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


# ---------------------------------------------------------------- エージェントのツール（読むだけ）
# 承認・却下（decide）はツールにしない。人が画面の承認タブで決めるのが HITL の線で、チャットからは決めさせない（2026-09-18）
def _tool_list_proposals(status: str = "all", limit: int = 20, device_id: str = "") -> dict:
    """list_proposals の既定値だけを変えたもの。画面は pending が既定だが、チャットで聞かれるのはたいてい履歴なので all"""
    return list_proposals(status=status, limit=limit, device_id=device_id)


TOOL_SPECS = [
    {"toolSpec": {
        "name": "list_proposals",
        "description": "AI が出した修復案と、その後の履歴（状態、原因、打ったコマンド、決めた人、実行結果、確認結果）。"
                       "「修復履歴は」「何を直した」「承認待ちは」と聞かれたらこれを呼ぶ。"
                       "状態は pending（承認待ち）→ approved / rejected（人が決めた）→ applied（実行した）→ verified（直ったのを確かめた）/ failed、expired（時間切れ）、obsolete（承認のあいだに異常が閉じたので打たなかった）。"
                       "承認や却下はこのツールではできない（人が画面の承認タブで決める）。",
        "inputSchema": {"json": {"type": "object", "properties": {
            "status": {"type": "string", "description": "all（全部、既定）か pending / approved / rejected / applied / verified / failed / expired / obsolete のどれか"},
            "limit": {"type": "integer", "description": "件数の上限（既定 20、最大 100）"},
            "device_id": {"type": "string", "description": "機器名（例 hq-ce-01）で絞る。空なら全機器"},
        }}},
    }},
]
TOOLS = {"list_proposals": _tool_list_proposals}
run_tool = toolkit.runner(TOOLS)
