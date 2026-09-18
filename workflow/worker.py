"""WORKFLOW のワーカー（Temporal on ECS Fargate）。
Spark が検知した異常（EventBridge → SQS）を受けてエージェントに原因を調べさせ、修復案を出し、人が承認したら lab EC2 で直し、異常が消えるまで確かめる。

流れ: Spark が異常を検知 → EventBridge にイベント（AnomalyOpened）→ SQS → ここ（agent = Temporal のワークフロー）が
Neptune / S3 / OpenSearch / Prometheus を見て原因分析 → 修復の提案 → 人間の承認 → Temporal で実行。
同じタスクの中の temporal コンテナ（temporal server start-dev、SQLite）に localhost:7233 でつなぐ。
1 プロセスで 2 つを動かす:
  - starter: ANOMALY_QUEUE_URL があれば SQS を long polling（20 秒）し、届いた AnomalyOpened ごとに investigate-<anomaly_id> のワークフローを起こす。
    キューが無ければ従来どおり POLL_INTERVAL 秒ごとに anomalies テーブルの open を見る
    （同じ id は Temporal が二重起動を弾く。同じ first_seen の修復案が既にあれば起こさない。SQS のメッセージは起こしたあとで消す）
  - worker: ワークフロー InvestigateAnomaly とアクティビティを回す

ワークフローの段: get_anomaly → investigate（AgentCore Runtime に JSON で答えさせる）→ put_proposal（pending）
  → 人の判断を proposals テーブルで待つ（web の「承認」タブが status を approved / rejected に変える。シグナル decide でも通る）
  → approved なら apply_on_lab（SSM Run Command で `sudo lab <cmd>`。cmd は ALLOWED_ACTIONS だけ）→ applied / failed
  → verify（VERIFY_ATTEMPTS 回、30 秒おきに異常が resolved か見る）→ verified / failed
  APPROVAL_TIMEOUT_MINUTES 過ぎたら expired。rejected なら何もしない。
状態は全部 proposals テーブルに書くので、web は Temporal を知らなくてよい。
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import timedelta

ANOMALY_TABLE = os.environ.get("ANOMALY_TABLE", "")
ANOMALY_QUEUE_URL = os.environ.get("ANOMALY_QUEUE_URL", "")  # terraform/workflow の events.tf。空ならテーブルを polling
PROPOSAL_TABLE = os.environ.get("PROPOSAL_TABLE", "")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
LAB_INSTANCE_ID = os.environ.get("LAB_INSTANCE_ID", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TASK_QUEUE = os.environ.get("TASK_QUEUE", "netops-investigate")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
APPROVAL_TIMEOUT_MINUTES = int(os.environ.get("APPROVAL_TIMEOUT_MINUTES", "120"))
VERIFY_ATTEMPTS = int(os.environ.get("VERIFY_ATTEMPTS", "6"))
VERIFY_INTERVAL = int(os.environ.get("VERIFY_INTERVAL", "30"))
DECISION_POLL = int(os.environ.get("DECISION_POLL", "30"))

# lab EC2 で打ってよいのはこれだけ（lab/lab.sh のサブコマンド）。エージェントが他を言っても none 扱いにする
ALLOWED_ACTIONS = {"heal-main": "sudo lab heal-main", "check": "sudo lab check"}
NO_ACTION = "none"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")


# ---------------------------------------------------------------- pure helpers (tests/test_workflow.py で確かめる)
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


def dedupe_key(anomaly: dict) -> tuple[str, int]:
    return anomaly.get("anomaly_id", ""), int(anomaly.get("first_seen") or 0)


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


# ---------------------------------------------------------------- AWS side (activities)
def _boto(name):
    import boto3

    return boto3.client(name, region_name=REGION)


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


def _typed(value):
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return {"S": str(value) if str(value) else "-"}


def list_open_anomalies(limit: int = 50) -> list:
    res = _boto("dynamodb").query(
        TableName=ANOMALY_TABLE, IndexName="status-last_seen-index", KeyConditionExpression="#s = :s",
        ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": {"S": "open"}},
        ScanIndexForward=False, Limit=limit)
    return [_plain(i) for i in res.get("Items", [])]


def read_anomaly(anomaly_id: str) -> dict:
    res = _boto("dynamodb").get_item(TableName=ANOMALY_TABLE, Key={"anomaly_id": {"S": anomaly_id}})
    return _plain(res["Item"]) if "Item" in res else {}


def read_proposal(proposal_id: str) -> dict:
    res = _boto("dynamodb").get_item(TableName=PROPOSAL_TABLE, Key={"proposal_id": {"S": proposal_id}}, ConsistentRead=True)
    return _plain(res["Item"]) if "Item" in res else {}


def write_proposal(item: dict) -> None:
    _boto("dynamodb").put_item(TableName=PROPOSAL_TABLE, Item={k: _typed(v) for k, v in item.items() if v is not None})


def update_proposal(proposal_id: str, fields: dict) -> None:
    fields = {**fields, "updated_at": int(time.time())}
    names = {f"#{i}": k for i, k in enumerate(fields)}
    values = {f":{i}": _typed(v) for i, v in enumerate(fields.values())}
    _boto("dynamodb").update_item(
        TableName=PROPOSAL_TABLE, Key={"proposal_id": {"S": proposal_id}},
        UpdateExpression="SET " + ", ".join(f"{n} = :{n[1:]}" for n in names),
        ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def ask_agent(prompt: str) -> str:
    session_id = f"workflow-{uuid.uuid4()}"  # runtimeSessionId は 33 文字以上
    res = _boto("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN, runtimeSessionId=session_id, qualifier="DEFAULT",
        contentType="application/json", accept="application/json",
        payload=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"))
    data = json.loads(res["response"].read())
    if not isinstance(data, dict) or data.get("status") != "success":
        raise RuntimeError(f"agent error: {str(data)[:300]}")
    return data.get("response", "")


def run_on_lab(command: str, timeout: int = 120) -> tuple[str, str]:
    """SSM Run Command で lab EC2 に 1 行打ち、(status, output) を返す"""
    ssm = _boto("ssm")
    cmd = ssm.send_command(
        InstanceIds=[LAB_INSTANCE_ID], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command], "executionTimeout": [str(timeout)]}, TimeoutSeconds=60)
    cid = cmd["Command"]["CommandId"]
    deadline = time.time() + timeout + 30
    while time.time() < deadline:
        time.sleep(3)
        try:
            inv = ssm.get_command_invocation(CommandId=cid, InstanceId=LAB_INSTANCE_ID)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if inv["Status"] in ("Pending", "InProgress", "Delayed"):
            continue
        out = (inv.get("StandardOutputContent", "") + inv.get("StandardErrorContent", ""))[:4000]
        return inv["Status"], out
    return "TimedOut", ""


# ---------------------------------------------------------------- Temporal
from temporalio import activity, workflow  # noqa: E402
from temporalio.client import Client, WorkflowFailureError  # noqa: E402
from temporalio.common import RetryPolicy  # noqa: E402
from temporalio.exceptions import WorkflowAlreadyStartedError  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))


@activity.defn
async def get_anomaly(anomaly_id: str) -> dict:
    a = await asyncio.to_thread(read_anomaly, anomaly_id)
    if not a:
        raise RuntimeError(f"anomaly {anomaly_id} not found")
    return a


@activity.defn
async def investigate(anomaly: dict) -> dict:
    text = await asyncio.to_thread(ask_agent, build_prompt(anomaly))
    parsed = parse_agent_json(text)
    action, command = normalize_action(parsed["action"])
    return {"cause": parsed["cause"], "action": action, "command": command,
            "reason": parsed["reason"], "agent_response": text[:4000]}


@activity.defn
async def put_proposal(anomaly: dict, finding: dict, wf_id: str) -> str:
    now = int(time.time())
    pid = anomaly["anomaly_id"]
    await asyncio.to_thread(write_proposal, {
        "proposal_id": pid, "anomaly_id": pid, "device_id": anomaly.get("device_id", ""),
        "kind": anomaly.get("kind", ""), "target": anomaly.get("target", ""),
        "first_seen": int(anomaly.get("first_seen") or 0), "status": "pending",
        "cause": finding["cause"], "action": finding["action"], "command": finding["command"],
        "reason": finding["reason"], "agent_response": finding["agent_response"],
        "workflow_id": wf_id, "created_at": now, "updated_at": now,
    })
    return pid


@activity.defn
async def get_decision(proposal_id: str) -> str:
    p = await asyncio.to_thread(read_proposal, proposal_id)
    return p.get("status", "pending")


@activity.defn
async def set_status(proposal_id: str, status: str, fields: dict | None = None) -> None:
    await asyncio.to_thread(update_proposal, proposal_id, {"status": status, **(fields or {})})


@activity.defn
async def apply_on_lab(command: str) -> dict:
    if not LAB_INSTANCE_ID:
        return {"status": "Skipped", "output": "LAB_INSTANCE_ID が無い（terraform/pipeline/lab が無い）"}
    status, out = await asyncio.to_thread(run_on_lab, command)
    return {"status": status, "output": out}


@activity.defn
async def anomaly_resolved(anomaly_id: str) -> bool:
    a = await asyncio.to_thread(read_anomaly, anomaly_id)
    return a.get("status") == "resolved"


@workflow.defn
class InvestigateAnomaly:
    def __init__(self) -> None:
        self._decision = ""

    @workflow.signal
    def decide(self, decision: str) -> None:
        if decision in ("approved", "rejected"):
            self._decision = decision

    @workflow.run
    async def run(self, anomaly_id: str) -> str:
        opts = {"start_to_close_timeout": timedelta(seconds=60), "retry_policy": RETRY}
        anomaly = await workflow.execute_activity(get_anomaly, anomaly_id, **opts)
        finding = await workflow.execute_activity(
            investigate, anomaly, start_to_close_timeout=timedelta(minutes=3), retry_policy=RETRY)
        pid = await workflow.execute_activity(put_proposal, args=[anomaly, finding, workflow.info().workflow_id], **opts)

        workflow.logger.info("proposal %s: pending (action=%s)", pid, finding["action"])

        # 人の判断を待つ（テーブルの status か、シグナル decide）
        deadline = workflow.now() + timedelta(minutes=APPROVAL_TIMEOUT_MINUTES)
        decision = ""
        while workflow.now() < deadline:
            # wait_condition は timeout に達すると asyncio.TimeoutError を投げ、それをそのまま漏らすと
            # ワークフロー自体が失敗する（temporalio は TimeoutError をタスク失敗でなくワークフロー失敗にする。
            # 2026-09-18 に承認しても applied に進まない原因だった）。時間切れは「まだ決まっていない」なので握って表を見る
            try:
                await workflow.wait_condition(lambda: bool(self._decision), timeout=timedelta(seconds=DECISION_POLL))
            except asyncio.TimeoutError:
                pass
            if self._decision:
                decision = self._decision
                break
            status = await workflow.execute_activity(get_decision, pid, **opts)
            if status in ("approved", "rejected"):
                decision = status
                break
        if not decision:
            workflow.logger.info("proposal %s: expired", pid)
            await workflow.execute_activity(set_status, args=[pid, "expired", {"verify_note": "承認待ちのまま時間切れ"}], **opts)
            return "expired"
        workflow.logger.info("proposal %s: %s", pid, decision)
        if decision == "rejected":
            return "rejected"

        # 承認された。none なら打つものが無いので applied 扱いで verify へ
        if finding["command"]:
            result = await workflow.execute_activity(
                apply_on_lab, finding["command"], start_to_close_timeout=timedelta(minutes=4), retry_policy=RetryPolicy(maximum_attempts=1))
            ok = result["status"] in ("Success", "Skipped")
            workflow.logger.info("proposal %s: apply %s -> %s", pid, finding["command"], result["status"])
            await workflow.execute_activity(
                set_status, args=[pid, "applied" if ok else "failed", {"apply_output": f"{result['status']}: {result['output']}"[:4000]}], **opts)
            if not ok:
                return "failed"
        else:
            await workflow.execute_activity(set_status, args=[pid, "applied", {"apply_output": "処置なし（action=none）"}], **opts)

        for i in range(VERIFY_ATTEMPTS):
            await asyncio.sleep(VERIFY_INTERVAL)
            if await workflow.execute_activity(anomaly_resolved, anomaly_id, **opts):
                workflow.logger.info("proposal %s: verified (%d)", pid, i + 1)
                await workflow.execute_activity(
                    set_status, args=[pid, "verified", {"verify_note": f"{i + 1} 回目の確認で resolved"}], **opts)
                return "verified"
        workflow.logger.info("proposal %s: still open after %d checks", pid, VERIFY_ATTEMPTS)
        await workflow.execute_activity(
            set_status, args=[pid, "failed", {"verify_note": f"{VERIFY_ATTEMPTS} 回確かめても open のまま"}], **opts)
        return "failed"


async def start_for(client: Client, anomaly: dict) -> bool:
    """1 つの異常についてワークフローを起こす（起こさない理由があれば False）"""
    existing = await asyncio.to_thread(read_proposal, anomaly.get("anomaly_id", ""))
    if not should_start(anomaly, existing):
        return False
    try:
        await client.start_workflow(InvestigateAnomaly.run, anomaly["anomaly_id"],
                                    id=workflow_id(anomaly["anomaly_id"]), task_queue=TASK_QUEUE)
        log.info("started %s", workflow_id(anomaly["anomaly_id"]))
        return True
    except WorkflowAlreadyStartedError:
        return False


def receive_messages() -> list:
    return _boto("sqs").receive_message(QueueUrl=ANOMALY_QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=20).get("Messages", [])


def delete_message(receipt: str) -> None:
    _boto("sqs").delete_message(QueueUrl=ANOMALY_QUEUE_URL, ReceiptHandle=receipt)


async def starter_queue(client: Client) -> None:
    """SQS（EventBridge のルールが流す AnomalyOpened）を待つ。受け取ったら anomalies テーブルの最新を読んで起こし、メッセージを消す"""
    for m in await asyncio.to_thread(receive_messages):
        aid = anomaly_id_from_message(m.get("Body", ""))
        if aid:
            a = await asyncio.to_thread(read_anomaly, aid)
            if a:
                await start_for(client, a)
            else:
                log.warning("starter: anomaly %s がテーブルに無い（メッセージは消す）", aid)
        else:
            log.warning("starter: 読めないメッセージ（消す）: %s", str(m.get("Body", ""))[:200])
        await asyncio.to_thread(delete_message, m["ReceiptHandle"])


async def starter_table(client: Client) -> None:
    """キューが無いとき（terraform/workflow の events.tf を配備していない）は open の一覧を polling"""
    for a in await asyncio.to_thread(list_open_anomalies):
        await start_for(client, a)
    await asyncio.sleep(POLL_INTERVAL)


async def starter(client: Client) -> None:
    once = starter_queue if ANOMALY_QUEUE_URL else starter_table
    while True:
        try:
            await once(client)
        except (WorkflowFailureError, RuntimeError, OSError) as e:
            log.warning("starter: %s", str(e)[:300])
            await asyncio.sleep(5)
        except Exception as e:  # noqa: BLE001 - boto の例外は種類が多いので落とさずログに出す
            log.warning("starter: %s: %s", type(e).__name__, str(e)[:300])
            await asyncio.sleep(5)


async def connect() -> Client:
    for i in range(60):
        try:
            return await Client.connect(TEMPORAL_ADDRESS)
        except Exception as e:  # noqa: BLE001 - temporal が起きるまで待つ
            log.info("waiting for temporal (%s): %s", TEMPORAL_ADDRESS, str(e)[:100])
            await asyncio.sleep(5)
    raise RuntimeError("temporal did not come up")


async def main() -> None:
    for k, v in (("ANOMALY_TABLE", ANOMALY_TABLE), ("PROPOSAL_TABLE", PROPOSAL_TABLE), ("AGENT_RUNTIME_ARN", AGENT_RUNTIME_ARN)):
        if not v:
            raise SystemExit(f"{k} が無い")
    client = await connect()
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[InvestigateAnomaly],
                    activities=[get_anomaly, investigate, put_proposal, get_decision, set_status, apply_on_lab, anomaly_resolved])
    log.info("worker up: queue=%s source=%s poll=%ss approval_timeout=%smin lab=%s", TASK_QUEUE,
             "sqs" if ANOMALY_QUEUE_URL else "table", POLL_INTERVAL, APPROVAL_TIMEOUT_MINUTES, LAB_INSTANCE_ID or "-")
    await asyncio.gather(worker.run(), starter(client))


if __name__ == "__main__":
    asyncio.run(main())
