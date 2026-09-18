"""異常一覧（DynamoDB。terraform/pipeline/analytics の Spark ジョブ（spark/snmp_sinks.py の detect）が書く。2026-09-17 までは stream の detector Lambda）をエージェントのツールと画面に出す。

テーブル名は環境変数 ANOMALY_TABLE、無ければ SSM の <PARAM_PREFIX>/anomaly-table（terraform/pipeline/stream が書く）。
どちらも無ければ「まだ配備されていない」を返して、PIPELINE を作っていない構成でも落ちない。
項目: anomaly_id（<機器>#<種別>#<対象>）, device_id, kind（link_down / trap）, target, status（open / resolved）,
first_seen / last_seen / resolved_at（epoch 秒）, source（poll / trap）, detail。
DynamoDB の読み方（クライアントの使い回し・表名・整形）は agent/toolkit.py に置いてある（proposals.py と共通）。
"""

from botocore.exceptions import BotoCoreError, ClientError

import toolkit

INDEX = "status-last_seen-index"  # GSI。パーティションキーが status、ソートキーが last_seen
STATUSES = ("open", "resolved")
QUERYABLE = STATUSES + ("all",)  # ツールと画面が指定できる値（all は両方）
TABLE = toolkit.Param("ANOMALY_TABLE", "anomaly-table")  # 表の名前（環境変数か SSM）


def _query(client, table: str, status: str, limit: int) -> list:
    """GSI をその status だけ、新しい順（ScanIndexForward=False）に limit 件まで"""
    res = client.query(
        TableName=table, IndexName=INDEX, KeyConditionExpression="#s = :s",
        ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": {"S": status}},
        ScanIndexForward=False, Limit=limit)
    return res.get("Items", [])


def list_anomalies(status: str = "open", limit: int = 20, device_id: str = "") -> dict:
    """status（open / resolved / all）の異常を新しい順に。Spark が最後に見た時刻（last_seen）で並ぶ。

    all は「これまでの異常は」に答えるためのもの（2026-09-18）。GSI のパーティションキーが status なので
    1 回の Query では両方取れない。open と resolved を別々に引いて last_seen で並べ直す（読むのは最大 2×read 件）。
    Limit は機器の絞り込みより先に効くので、device_id があるときは多めに読んでから絞る。
    """
    table = TABLE.value()
    if not table:
        return {"error": "異常一覧はまだ配備されていない（terraform/pipeline/stream を apply すると使える）", "anomalies": []}
    status = status if status in QUERYABLE else "open"
    limit = max(1, min(int(limit), 100))
    read = toolkit.read_count(device_id, limit)
    client = toolkit.client("dynamodb")
    try:
        if status == "all":
            items = _query(client, table, "open", read) + _query(client, table, "resolved", read)
            items.sort(key=lambda i: int(i.get("last_seen", {}).get("N", "0")), reverse=True)
        else:
            items = _query(client, table, status, read)
    except (ClientError, BotoCoreError) as e:
        return {"error": f"異常一覧を読めなかった: {str(e)[:200]}", "anomalies": []}
    rows = []
    for it in toolkit.narrow(items, device_id, limit):  # 返す行だけを整形する
        r = toolkit.plain(it)
        r["first_seen_jst"] = toolkit.jst(r.get("first_seen"))
        r["last_seen_jst"] = toolkit.jst(r.get("last_seen"))
        if r.get("resolved_at"):
            r["resolved_at_jst"] = toolkit.jst(r["resolved_at"])
        rows.append(r)
    return {"status": status, "count": len(rows), "anomalies": rows}


TOOL_SPECS = [
    {"toolSpec": {
        "name": "list_anomalies",
        "description": "監視で見つかった異常の一覧（機器、種別 link_down / trap、対象インタフェース、発生時刻、最後に確認した時刻、解消時刻、poll か trap か）。"
                       "「今の異常は」「どこが落ちている」と聞かれたら status=open で呼ぶ。"
                       "「これまでの異常は」「過去に何があった」「履歴」と聞かれたら status=all（解消済みも含む）で呼ぶ。解消済みだけなら status=resolved。",
        "inputSchema": {"json": {"type": "object", "properties": {
            "status": {"type": "string", "description": "open（未解消、既定）／ resolved（解消済み）／ all（両方を新しい順に）"},
            "limit": {"type": "integer", "description": "件数の上限（既定 20、最大 100）"},
            "device_id": {"type": "string", "description": "機器名（例 hq-ce-01）で絞る。空なら全機器"},
        }}},
    }},
]
TOOLS = {"list_anomalies": list_anomalies}
run_tool = toolkit.runner(TOOLS)
