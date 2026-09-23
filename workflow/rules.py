"""ワーカーの「判断」の部分。AWS にも Temporal にも触らない純粋な関数だけを置く。

ここにあるのは 5 つ:
  - build_prompt        エージェント（AgentCore Runtime）に投げる質問文
  - parse_agent_json    返ってきた文から JSON を取り出す
  - normalize_action    lab EC2 で打ってよいコマンドの許可リスト
  - should_start / workflow_id / proposal_id / event_from_message  どの異常でワークフローを起こすかの判定と、発生ごとの id
  - proposal_event      修復案の証跡（S3 Tables の proposal_events）の 1 行

AWS も Temporal も要らないので、tests/test_workflow.py はこのファイルの関数を直接呼んで確かめられる。
逆に言うと、ここに boto3 や temporalio を持ち込むとテストが動かなくなる。入れない。
"""

import json
import re

# lab EC2 で打ってよいのはこれだけ（lab/lab.sh のサブコマンド）。エージェントが他を言っても none 扱いにする
ALLOWED_ACTIONS = {"heal-main": "sudo lab heal-main", "check": "sudo lab check"}
NO_ACTION = "none"


def build_prompt(anomaly: dict) -> str:
    """エージェントに投げる質問。答えは JSON 1 個だけにさせる"""
    return (
        "あなたはネットワーク運用の一次切り分け担当です。次の異常について、ツールで状況を確かめてから、原因と処置を JSON で 1 つだけ返してください。"
        "トポロジと影響範囲は neighbors / blast_radius（Neptune）、他の異常は list_anomalies、その機器のログは search_logs（OpenSearch）、"
        "メトリクスの推移は query_metrics（Prometheus）、長期の履歴は query_history（S3）で見て、見えた事実だけを根拠に原因を書いてください。"
        "説明文や Markdown は付けないでください。\n"
        f"異常: device_id={anomaly.get('device_id', '')} kind={anomaly.get('kind', '')} target={anomaly.get('target', '')} "
        f"detail={anomaly.get('detail', '')} first_seen_jst={anomaly.get('first_seen_jst', '')}\n"
        '返す形: {"cause": "原因（日本語 1〜2 文）", "action": "heal-main | check | none", "reason": "その処置を選んだ理由"}\n'
        "action は、本社 hq-ce-01 の eth1 が落ちている（link_down）なら heal-main、状況を見るだけでよいなら check、"
        "人が別の手で直すべきなら none。"
    )


def parse_agent_json(text: str) -> dict:
    """応答の中の最初の {...} を JSON として読む。読めなければ action=none で理由に生の文を入れる"""
    text = text or ""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return {
                    "cause": str(data.get("cause", ""))[:1000],
                    "action": str(data.get("action", NO_ACTION)).strip(),
                    "reason": str(data.get("reason", ""))[:1000],
                }
        except ValueError:
            pass
    return {"cause": "", "action": NO_ACTION, "reason": text[:1000]}


def normalize_action(action: str) -> tuple[str, str]:
    """(action, command)。許可リストに無いものは none（コマンド空）"""
    action = (action or "").strip()
    if action in ALLOWED_ACTIONS:
        return action, ALLOWED_ACTIONS[action]
    return NO_ACTION, ""


# ワークフローを起こす異常の種類。trap（coldStart 以外の通知）は TTL で閉じるだけの「見えた」印で、打つ処置も無いので起こさない
START_KINDS = {"link_down"}


def should_start(anomaly: dict, existing: dict | None) -> bool:
    """open の link_down で、同じ発生（anomaly_id + first_seen）の修復案がまだ無ければ起こす。
    Spark（spark/snmp_sinks.py の detect）は resolved から開き直すと first_seen を今にするので、開き直せば別の発生として起こし直す。
    修復案は発生ごとの id（proposal_id）で引くので、existing があれば同じ発生（first_seen の比較は古い id の行のための保険）"""
    if not anomaly.get("anomaly_id") or anomaly.get("kind") not in START_KINDS or anomaly.get("status", "open") != "open":
        return False
    if not existing:
        return True
    return int(existing.get("first_seen") or 0) != int(anomaly.get("first_seen") or 0)


def occurrence(anomaly_id: str, first_seen) -> str:
    """1 回の発生の id。anomaly_id だけだと、閉じて開き直した次の発生が前の修復案とワークフローに重なる（2026-09-24）"""
    return f"{anomaly_id}#{int(first_seen or 0)}"


def workflow_id(anomaly_id: str, first_seen) -> str:
    return f"investigate-{occurrence(anomaly_id, first_seen)}"


def proposal_id(anomaly_id: str, first_seen) -> str:
    return occurrence(anomaly_id, first_seen)


def event_from_message(body: str) -> tuple[str, int]:
    """SQS のメッセージ本文（EventBridge のイベントそのまま）から (anomaly_id, first_seen) を取る。読めなければ ("", 0)"""
    try:
        data = json.loads(body or "")
    except ValueError:
        return "", 0
    if not isinstance(data, dict):
        return "", 0
    detail = data.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            return "", 0
    if not isinstance(detail, dict):
        return "", 0
    try:
        first_seen = int(detail.get("first_seen") or 0)
    except (TypeError, ValueError):
        first_seen = 0
    return str(detail.get("anomaly_id") or ""), first_seen


def anomaly_id_from_message(body: str) -> str:
    return event_from_message(body)[0]


# ---------------------------------------------------------------- 修復案の証跡（S3 Tables の proposal_events。2026-09-24）
# 列は terraform/pipeline/analytics/tables.tf の proposal_events と同じ順・同じ型。時刻は epoch 秒で組み、awsio が書くときに tz 付きにする
PROPOSAL_EVENT_COLUMNS = (
    ("event_id", "string"), ("proposal_id", "string"), ("anomaly_id", "string"), ("event", "string"), ("status", "string"),
    ("device_id", "string"), ("action", "string"), ("cause", "string"), ("command", "string"), ("decided_by", "string"),
    ("detail", "string"), ("event_time", "timestamptz"),
)
# created は pending で置いたとき。ほかは status の移り変わりそのもの
PROPOSAL_EVENTS = ("created", "approved", "rejected", "expired", "obsolete", "applied", "failed", "verified")


def proposal_event(event: str, proposal: dict, now: int, detail: str = "", decided_by: str = "") -> dict:
    """proposal_events の 1 行。event_id = <proposal_id>#<event>（1 つの修復案で同じ出来事は 1 回だけ。
    アクティビティの再試行で二重に入ったら event_id で重複を落とす）"""
    if event not in PROPOSAL_EVENTS:
        raise ValueError(f"unknown proposal event: {event}")
    pid = str(proposal.get("proposal_id") or "")
    return {
        "event_id": f"{pid}#{event}", "proposal_id": pid, "anomaly_id": str(proposal.get("anomaly_id") or ""),
        "event": event, "status": "pending" if event == "created" else event,
        "device_id": str(proposal.get("device_id") or ""), "action": str(proposal.get("action") or ""),
        "cause": str(proposal.get("cause") or ""), "command": str(proposal.get("command") or ""),
        "decided_by": str(decided_by or proposal.get("decided_by") or ""), "detail": str(detail or "")[:4000],
        "event_time": int(now),
    }
