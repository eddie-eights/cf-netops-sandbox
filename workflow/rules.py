"""ワーカーの「判断」の部分。AWS にも Temporal にも触らない純粋な関数だけを置く。

ここにあるのは 5 つ:
  - build_prompt        エージェント（AgentCore Runtime）に投げる質問文
  - parse_agent_json    返ってきた文から JSON を取り出す
  - normalize_action    lab EC2 で打ってよいコマンドの許可リスト
  - alerts_from_message / should_start / workflow_id / proposal_id  アラート（SNS → SQS）の読み取りと、どれでワークフローを起こすかの判定と id
  - proposal_event      修復案の証跡（S3 Tables の proposal_events）の 1 行

AWS も Temporal も要らないので、tests/test_workflow.py はこのファイルの関数を直接呼んで確かめられる。
逆に言うと、ここに boto3 や temporalio を持ち込むとテストが動かなくなる。入れない。
"""

import json
import re
from datetime import datetime, timedelta, timezone

# lab EC2 で打ってよいのはこれだけ（lab/lab.sh のサブコマンド）。エージェントが他を言っても none 扱いにする
ALLOWED_ACTIONS = {"heal-main": "sudo lab heal-main", "check": "sudo lab check"}
NO_ACTION = "none"
JST = timezone(timedelta(hours=9))


def jst(epoch) -> str:
    """epoch 秒を日本時間の「2026-09-18 12:34:56」に（agent/toolkit.py の jst と同じ形）。空や 0 なら空文字"""
    return datetime.fromtimestamp(int(epoch), JST).strftime("%Y-%m-%d %H:%M:%S") if epoch else ""


def build_prompt(anomaly: dict) -> str:
    """エージェントに投げる質問。答えは JSON 1 個だけにさせる"""
    return (
        "あなたはネットワーク運用の一次切り分け担当です。次の異常について、ツールで状況を確かめてから、原因と処置を JSON で 1 つだけ返してください。"
        "トポロジと影響範囲は neighbors / blast_radius（Neptune。回線や機器の status が DOWN / ALARM なら他にも落ちている）、その機器のログは search_logs（OpenSearch）、"
        "メトリクスの推移は query_metrics（Prometheus）、長期の履歴は query_history（S3）で見て、見えた事実だけを根拠に原因を書いてください。"
        "説明文や Markdown は付けないでください。\n"
        f"異常: device_id={anomaly.get('device_id', '')} kind={anomaly.get('kind', '')} target={anomaly.get('target', '')} "
        f"detail={anomaly.get('detail', '')} first_seen_jst={jst(anomaly.get('first_seen'))}\n"
        '返す形: {"cause": "原因（日本語 1〜2 文）", "action": "heal-main | check | none", "reason": "その処置を選んだ理由"}\n'
        "action は、アクセス側 Leaf dc1-leaf-01 の ethernet-1/1（dc1-spine-01 との fabric）が落ちている（link_down か、その上の isis_down）なら heal-main、状況を見るだけでよいなら check、"
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


# ---------------------------------------------------------------- アラート（Grafana / Splunk → SNS → SQS）
# SNS に publish する JSON は送り手（grafana/provisioning/alerting と splunk/netops_alerts）で形を揃えてある:
#   {"source": "grafana" | "splunk",
#    "alerts": [{"status": "firing" | "resolved", "device_id": "dc1-leaf-01", "kind": "link_down", "target": "ethernet-1/1",
#                "detail": "ethernet-1/1 is down", "starts_at": 1790000000}]}
# 同じ形を graph/status_handler.py（トポロジの status を書く Lambda）も読む。形を変えるときは 4 か所を一緒に変える
ALERT_STATUSES = ("firing", "resolved")
# ワークフローを起こす異常の種類。trap（linkDown / linkUp 以外の通知）は「見えた」印で、打つ処置も無いので起こさない
START_KINDS = {"link_down"}
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def device_name(name) -> str:
    """機器名をトポロジの device_id（小文字の短い名前）に揃える。FQDN はドメインを落とす（IPv4 はそのまま）"""
    name = str(name or "").strip().lower()
    return name if IPV4_RE.match(name) else name.split(".", 1)[0]


def anomaly_id(device_id: str, kind: str, target: str) -> str:
    """異常の id。送り手が違っても同じ機器・種類・対象なら同じ id（Splunk と Grafana の両方が同じ障害を知らせても 1 つにまとまる）"""
    return f"{device_id}#{kind}#{target}"


def _epoch(v) -> int:
    try:
        return max(int(float(v or 0)), 0)
    except (TypeError, ValueError):
        return 0


def alerts_from_message(body: str, now: int = 0) -> list:
    """SQS のメッセージ本文（SNS に publish された JSON そのまま。購読は raw message delivery）からアラートの list を取る。
    1 件 = {anomaly_id, device_id, kind, target, detail, status, first_seen, source}。first_seen は starts_at（無ければ now）。
    読めない本文は []、形の合わない要素（機器か種類が無い・status が firing / resolved でない）は捨てる。
    raw でない配り方（{"Type": "Notification", "Message": "…"} の封筒）でも中身を読む"""
    try:
        data = json.loads(body or "")
    except ValueError:
        return []
    if isinstance(data, dict) and data.get("Type") == "Notification" and isinstance(data.get("Message"), str):
        try:
            data = json.loads(data["Message"])
        except ValueError:
            return []
    if not isinstance(data, dict) or not isinstance(data.get("alerts"), list):
        return []
    out = []
    for a in data["alerts"]:
        if not isinstance(a, dict):
            continue
        dev, kind = device_name(a.get("device_id")), str(a.get("kind") or "").strip()
        target, status = str(a.get("target") or "").strip(), str(a.get("status") or "").strip().lower()
        if not dev or not kind or status not in ALERT_STATUSES:
            continue
        out.append({
            "anomaly_id": anomaly_id(dev, kind, target), "device_id": dev, "kind": kind, "target": target,
            "detail": str(a.get("detail") or "")[:1000], "status": status,
            "first_seen": _epoch(a.get("starts_at")) or int(now or 0), "source": str(data.get("source") or "")[:40],
        })
    return out


def should_start(alert: dict, existing: dict | None) -> bool:
    """firing の link_down で、同じ発生（anomaly_id + first_seen）の修復案がまだ無ければ起こす。
    Grafana は同じ発生の通知を repeat_interval ごとに同じ starts_at で送り直すので、修復案があればその発生はもう扱っている"""
    if not alert.get("anomaly_id") or alert.get("kind") not in START_KINDS or alert.get("status") != "firing":
        return False
    return not existing


def workflow_id(anomaly_id: str) -> str:
    """ワークフローの id は異常ごと（発生の時刻を入れない）。送り手ごとに starts_at が違うので、入れると Splunk と Grafana の
    同じ障害が別のワークフローになる。同じ id が走っているあいだ、Temporal が二重起動を弾く"""
    return f"investigate-{anomaly_id}"


def proposal_id(anomaly_id: str, first_seen) -> str:
    """修復案の id は発生ごと。anomaly_id だけだと、閉じて開き直した次の発生が前の修復案に重なる（2026-09-24）"""
    return f"{anomaly_id}#{int(first_seen or 0)}"


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
