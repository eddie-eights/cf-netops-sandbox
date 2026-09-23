"""修復案（Neptune の頂点 label=proposal。terraform/workflow のワーカーが書く）を画面に出し、人の承認・却下を書き戻す。

2026-09-24 までは DynamoDB の表（terraform/workflow）だった。いまは Neptune（terraform/pipeline/graph）の頂点で、読み書きは agent/graph.py の
list_records / get_record / update_record。Neptune の接続先が無ければ「まだ配備されていない」を返して、WORKFLOW を作っていない構成でも落ちない。
頂点: id = proposal_id（= <anomaly_id>#<first_seen>。発生ごとに 1 件。閉じて開き直した次の発生は別の頂点）, anomaly_id, device_id, kind, target,
first_seen（異常の発生時刻）, status（pending → approved / rejected（人）→ applied → verified / failed（ワーカー）、expired（時間切れ）、
obsolete（承認のあいだに異常が閉じた・開き直したので打たなかった））,
cause, action（heal-main / check / none）, command, reason, agent_response, workflow_id,
created_at / updated_at / decided_at（epoch 秒）, decided_by, apply_output, verify_note。
承認・却下は status = pending のときだけ通る（has('status','pending') と property が 1 本の Gremlin）。ワーカーは Temporal のシグナルではなく、
この頂点の status をポーリングして進む（画面と Temporal を直接つながない）。作成・承認・却下・適用・確認の履歴はワーカーが
S3 Tables の proposal_events に 1 行ずつ残す（workflow/worker.py。画面は S3 Tables に触らない）。

Neptune の書き込み許可は Runtime にも付いている（terraform/pipeline/graph の access.tf。頂点のラベル単位では絞れない）。
チャットから承認させない（HITL）のはコードの線で、decide をエージェントのツール（TOOL_SPECS）に出さないことで守る。
"""

import time

from botocore.exceptions import BotoCoreError, ClientError

import graph
import toolkit

STATUSES = ("pending", "approved", "rejected", "applied", "verified", "failed", "expired", "obsolete")
QUERYABLE = STATUSES + ("all",)  # 一覧で指定できる値（all は全部）
DECISIONS = ("approved", "rejected")  # 人が決められるのはこの 2 つだけ
NOT_DEPLOYED = "修復案はまだ配備されていない（terraform/pipeline/graph と terraform/workflow を apply すると使える）"


def _decorate(p: dict) -> dict:
    """epoch 秒の項目に、読める形（JST）を並べて足す"""
    for k in ("created_at", "updated_at", "decided_at", "first_seen"):
        p[f"{k}_jst"] = toolkit.jst(p.get(k))
    return p


def list_proposals(status: str = "pending", limit: int = 50, device_id: str = "") -> dict:
    """status の修復案を新しい順に（updated_at）。status が all なら全部。device_id があればその機器だけ（Gremlin の中で絞る）"""
    if not graph.configured():
        return {"error": NOT_DEPLOYED, "proposals": []}
    status = status if status in QUERYABLE else "pending"
    limit = max(1, min(int(limit), 100))
    try:
        items = graph.list_records("proposal", "proposal_id", "updated_at", "" if status == "all" else status, device_id, limit)
    except (ClientError, BotoCoreError) as e:
        return {"error": f"修復案を読めない: {str(e)[:200]}", "proposals": []}
    proposals = [_decorate(p) for p in items]
    return {"status": status, "count": len(proposals), "proposals": proposals}


def get_proposal(proposal_id: str) -> dict:
    """1 件だけ引く（画面の承認タブが、決める直前の状態を確かめるのに使う）。無ければ空の辞書"""
    if not graph.configured() or not proposal_id:
        return {}
    try:
        p = graph.get_record("proposal", "proposal_id", proposal_id)
    except (ClientError, BotoCoreError):
        return {}
    return _decorate(p) if p else {}


def decide(proposal_id: str, decision: str, decided_by: str = "web") -> dict:
    """pending の修復案を approved / rejected にする。pending でなければ何もしない（誰かが先に決めた・ワーカーが進めた）"""
    if not graph.configured():
        return {"error": NOT_DEPLOYED}
    if decision not in DECISIONS:
        return {"error": f"decision は {' / '.join(DECISIONS)} のどれか"}
    if not proposal_id:
        return {"error": "proposal_id が空"}
    now = int(time.time())
    fields = {"status": decision, "decided_by": decided_by[:64], "decided_at": now, "updated_at": now}
    try:
        done = graph.update_record("proposal", proposal_id, fields, only_status="pending")
    except (ClientError, BotoCoreError) as e:
        return {"error": f"更新できない: {str(e)[:200]}"}
    if not done:
        return {"error": f"{proposal_id} は pending ではない（先に決まったか、ワーカーが進めた）"}
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
