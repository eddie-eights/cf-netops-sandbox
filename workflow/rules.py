"""ワーカーの「判断」の部分。AWS にも Temporal にも触らない純粋な関数だけを置く。

ここにあるのは 4 つ:
  - build_prompt        エージェント（AgentCore Runtime）に投げる質問文
  - parse_agent_json    返ってきた文から JSON を取り出す
  - normalize_action    lab EC2 で打ってよいコマンドの許可リスト
  - should_start / workflow_id / anomaly_id_from_message  どの異常でワークフローを起こすかの判定

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


def should_start(anomaly: dict, existing: dict | None) -> bool:
    """同じ異常（anomaly_id + first_seen）の修復案が既にあれば起こさない。無ければ起こす。
    Spark（spark/snmp_sinks.py の detect）は resolved から開き直すと first_seen を今にするので、同じ異常が resolved → 再 open になれば起こし直す
    （2026-09-18 まで first_seen が残っていて起こし直せなかった）"""
    if not anomaly.get("anomaly_id"):
        return False
    if not existing:
        return True
    return int(existing.get("first_seen") or 0) != int(anomaly.get("first_seen") or 0)


def workflow_id(anomaly_id: str) -> str:
    return f"investigate-{anomaly_id}"


def anomaly_id_from_message(body: str) -> str:
    """SQS のメッセージ本文（EventBridge のイベントそのまま）から anomaly_id を取る。読めなければ空"""
    try:
        data = json.loads(body or "")
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    detail = data.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            return ""
    if not isinstance(detail, dict):
        return ""
    return str(detail.get("anomaly_id") or "")
