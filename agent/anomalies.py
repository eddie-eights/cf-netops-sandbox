"""異常一覧（Neptune の頂点 label=anomaly。terraform/pipeline/analytics の Spark ジョブ（spark/snmp_sinks.py の detect）が書く）をエージェントのツールと画面に出す。

2026-09-24 までは DynamoDB の表（terraform/pipeline/stream）だった。いまは Neptune（terraform/pipeline/graph）の頂点で、読み方は agent/graph.py の list_records。
Neptune の接続先が無ければ（graph.configured() が False）「まだ配備されていない」を返して、PIPELINE を作っていない構成でも落ちない。
頂点: id = anomaly_id（<機器>#<種別>#<対象>）, device_id, kind（link_down / trap）, target, status（open / resolved）,
first_seen / last_seen / resolved_at（epoch 秒）, source（poll / trap）, detail, notified。
開いた・閉じたの履歴は S3 Tables の anomaly_events（Athena で読む。docs/data-stores.md）。ここが見せるのは発生ごとの「いま」だけ。
"""

from botocore.exceptions import BotoCoreError, ClientError

import graph
import toolkit

STATUSES = ("open", "resolved")
QUERYABLE = STATUSES + ("all",)  # ツールと画面が指定できる値（all は両方）


def list_anomalies(status: str = "open", limit: int = 20, device_id: str = "") -> dict:
    """status（open / resolved / all）の異常を新しい順に。Spark が最後に見た時刻（last_seen）で並ぶ。
    all は「これまでの異常は」に答えるためのもの（2026-09-18）。機器と status の絞り込みは Gremlin の中でやる（絞ってから limit 件）"""
    if not graph.configured():
        return {"error": "異常一覧はまだ配備されていない（terraform/pipeline/graph と analytics を apply すると使える）", "anomalies": []}
    status = status if status in QUERYABLE else "open"
    limit = max(1, min(int(limit), 100))
    try:
        items = graph.list_records("anomaly", "anomaly_id", "last_seen", "" if status == "all" else status, device_id, limit)
    except (ClientError, BotoCoreError) as e:
        return {"error": f"異常一覧を読めなかった: {str(e)[:200]}", "anomalies": []}
    for r in items:
        r["first_seen_jst"] = toolkit.jst(r.get("first_seen"))
        r["last_seen_jst"] = toolkit.jst(r.get("last_seen"))
        if r.get("resolved_at"):
            r["resolved_at_jst"] = toolkit.jst(r["resolved_at"])
    return {"status": status, "count": len(items), "anomalies": items}


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
