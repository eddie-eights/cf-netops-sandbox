"""「異常一覧」タブと「承認」タブの中身。どちらも Neptune の頂点を読む（承認タブは status も書き戻す）。

  異常一覧  terraform/pipeline/analytics の Spark が書いた anomaly の頂点（anomalies.py）
  承認      terraform/workflow のワーカーが書いた proposal の頂点（proposals.py）。承認・却下はここから書き戻す。履歴は S3 Tables の proposal_events（ワーカーが書く）

未配備のときは list_* が error を返すので、その文言をそのまま画面に出す。
"""

import html

import gradio as gr
import pandas as pd

import config  # noqa: F401 - sys.path と TOPOLOGY_DATA_DIR を通すために先に読む

import anomalies
import proposals

ANOMALY_COLS = ["機器", "種別", "対象", "状態", "発生", "最終確認", "解消", "経路"]
PROPOSAL_COLS = ["proposal_id", "状態", "機器", "種別", "対象", "原因", "処置", "コマンド", "理由", "作成", "更新", "決めた人", "結果"]

# 表は列が多く、原因・理由は長い文なので折り返す。幅は %（Gradio 5 の column_widths）。表で切れる分は下の「詳細」に全文を出す
PROPOSAL_WIDTHS = ["14%", "6%", "7%", "7%", "5%", "16%", "6%", "9%", "16%", "7%", "7%", "5%", "12%"]


# ---------------------------------------------------------------- 異常一覧
def anomaly_table(status: str = "open"):
    r = anomalies.list_anomalies(status=status, limit=100)
    rows = [{"機器": a.get("device_id", ""), "種別": a.get("kind", ""), "対象": a.get("target", ""), "状態": a.get("status", ""),
             "発生": a.get("first_seen_jst", ""), "最終確認": a.get("last_seen_jst", ""), "解消": a.get("resolved_at_jst", ""),
             "経路": a.get("source", "")} for a in r.get("anomalies", [])]
    msg = r["error"] if r.get("error") else f"{r.get('count', 0)} 件（{'未解消' if status == 'open' else '解消済み'}）"
    return msg, pd.DataFrame(rows, columns=ANOMALY_COLS)


# ---------------------------------------------------------------- 承認（WORKFLOW）
def proposal_table(status: str = "pending"):
    """表と、proposal_id の選択肢（表の 1 列目と同じ）"""
    r = proposals.list_proposals(status=status, limit=100)
    rows = [{"proposal_id": p.get("proposal_id", ""), "状態": p.get("status", ""), "機器": p.get("device_id", ""),
             "種別": p.get("kind", ""), "対象": p.get("target", ""), "原因": p.get("cause", ""), "処置": p.get("action", ""),
             "コマンド": p.get("command", ""), "理由": p.get("reason", ""), "作成": p.get("created_at_jst", ""),
             "更新": p.get("updated_at_jst", ""), "決めた人": p.get("decided_by", ""),
             "結果": (p.get("verify_note") or p.get("apply_output") or "")[:200]} for p in r.get("proposals", [])]
    msg = r["error"] if r.get("error") else f"{r.get('count', 0)} 件（{status}）"
    ids = [row["proposal_id"] for row in rows]
    # 選択は空に戻す。先頭を選んでおくと、表が描き直されて先頭が別の修復案に替わったとき、見ていない案をそのまま承認できてしまう
    return msg, pd.DataFrame(rows, columns=PROPOSAL_COLS), gr.update(choices=ids, value=None)


def proposal_detail(proposal_id: str) -> str:
    """選んだ修復案の全文（表では切れる原因・理由・結果）"""
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        return ""
    p = proposals.get_proposal(proposal_id)
    if not p:
        return f"`{proposal_id}` は無い（更新を押す）"
    lines = [f"**{html.escape(proposal_id)}** — {p.get('status', '')}（{p.get('device_id', '')} / {p.get('kind', '')} / {p.get('target', '')}）", ""]
    for label, key in (("原因", "cause"), ("処置", "action"), ("コマンド", "command"), ("理由", "reason"),
                       ("実行結果", "apply_output"), ("確認結果", "verify_note"), ("決めた人", "decided_by"),
                       ("作成", "created_at_jst"), ("更新", "updated_at_jst")):
        v = str(p.get(key) or "").strip()
        if v:
            lines.append(f"- **{label}**: {html.escape(v)}")
    return "\n".join(lines)


APPROVER_MAX = 40  # decided_by に残す名前の長さ（proposals.decide は 64 字で切る。「 (web)」を足しても収まる）


def decide_proposal(proposal_id: str, decision: str, status: str, approver: str = "", confirmed: bool = False):
    """承認・却下を書く。名前（decided_by に「<名前> (web)」で残す）は両方に要り、承認は「詳細を読んだ」の確認も要る。
    Web は SSM のポートフォワーディングの先で認証が無く、誰が押したかを画面の外から知る手段が無いので、自分で名乗ってもらう。
    足りなければ Neptune には触らず、表と選択もそのまま残す"""
    proposal_id = (proposal_id or "").strip()
    name = " ".join((approver or "").split())[:APPROVER_MAX]
    if not proposal_id:
        return "proposal_id を選ぶ（表の 1 列目）", gr.update(), gr.update()
    if not name:
        return "決める人の名前を入れる（decided_by に残る）", gr.update(), gr.update()
    if decision == "approved" and not confirmed:
        return "「詳細を読んだ」にチェックを入れてから承認する（lab でコマンドが打たれる）", gr.update(), gr.update()
    r = proposals.decide(proposal_id, decision, decided_by=f"{name} (web)")
    msg = r["error"] if r.get("error") else f"{r['proposal_id']} を {decision} にした（{name}。ワーカーが次の段に進める。状態を approved / applied / verified にして更新で追える）"
    _, table, ids = proposal_table(status)
    return msg, table, ids
