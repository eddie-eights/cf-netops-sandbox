"""WORKFLOW のワーカー（Temporal on ECS Fargate）。
Spark が検知した異常（EventBridge → SQS）を受けてエージェントに原因を調べさせ、修復案を出し、人が承認したら lab EC2 で直し、異常が消えるまで確かめる。

流れ: Spark が異常を検知 → EventBridge にイベント（AnomalyOpened）→ SQS → ここ（agent = Temporal のワークフロー）が
Neptune / S3 / OpenSearch / Prometheus を見て原因分析 → 修復の提案 → 人間の承認 → Temporal で実行。
同じタスクの中の temporal コンテナ（temporal server start-dev、SQLite）に localhost:7233 でつなぐ。
1 プロセスで 2 つを動かす:
  - starter: ANOMALY_QUEUE_URL があれば SQS を long polling（20 秒）し、届いた AnomalyOpened ごとに investigate-<anomaly_id>#<first_seen> の
    ワークフローを起こす（id は発生ごと。閉じて開き直した次の発生は別のワークフローと別の修復案になる）。
    キューが無ければ従来どおり POLL_INTERVAL 秒ごとに anomalies テーブルの open を見る
    （同じ id は Temporal が二重起動を弾く。同じ発生の修復案が既にあれば起こさない。起こすのは rules.START_KINDS（link_down）だけ）。
    SQS のメッセージは、起こした・起こす理由が無い・もう起きている（WorkflowAlreadyStartedError。同じ発生の重複配達）のどれかなら消し、
    それ以外の失敗（Temporal や DynamoDB に届かない）なら消さずに残して、可視性タイムアウトのあとで配り直させる
  - worker: ワークフロー InvestigateAnomaly とアクティビティを回す

ワークフローの段: get_anomaly（もう閉じた・別の発生なら何もせず obsolete で終わる）→ investigate（AgentCore Runtime に JSON で答えさせる）
  → put_proposal（pending。proposal_id = <anomaly_id>#<first_seen>、同じ id が既にあれば上書きしない）
  → 人の判断を proposals テーブルで待つ（web の「承認」タブが status を approved / rejected に変える。シグナル decide でも通る）
  → approved なら、打つ直前に同じ発生がまだ open か確かめる（閉じていれば obsolete にして打たない）
  → apply_on_lab（SSM Run Command で `sudo lab <cmd>`。cmd は rules.ALLOWED_ACTIONS だけ）→ applied / failed
  → verify（VERIFY_ATTEMPTS 回、30 秒おきに異常が resolved か見る）→ verified / failed
  APPROVAL_TIMEOUT_MINUTES 過ぎたら expired。rejected なら何もしない。
状態は全部 proposals テーブルに書くので、web は Temporal を知らなくてよい。

3 ファイルに分けてある（同じディレクトリに置いて import する。Dockerfile は workflow/*.py を全部入れる）:
  rules.py   判断だけの純粋関数（プロンプト・JSON の読み取り・許可コマンド・起こすかどうか）
  awsio.py   環境変数と AWS 呼び出し（DynamoDB / AgentCore / SSM / SQS）
  worker.py  ここ。Temporal のアクティビティ・ワークフロー・starter・main
"""

import asyncio
import logging
import os
import time
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

# Temporal はワークフローを決定的に保つため、@workflow.defn のあるこのモジュールをサンドボックスの中で再 import する。
# 自作モジュールはそのとき素通しにする（公式の作法）。素通しにしないと awsio / rules がサンドボックス用に作り直され、
# 同じ名前の別物になる
with workflow.unsafe.imports_passed_through():
    import awsio
    import rules

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TASK_QUEUE = os.environ.get("TASK_QUEUE", "netops-investigate")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
APPROVAL_TIMEOUT_MINUTES = int(os.environ.get("APPROVAL_TIMEOUT_MINUTES", "120"))
VERIFY_ATTEMPTS = int(os.environ.get("VERIFY_ATTEMPTS", "6"))
VERIFY_INTERVAL = int(os.environ.get("VERIFY_INTERVAL", "30"))
DECISION_POLL = int(os.environ.get("DECISION_POLL", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")

RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))


# ---------------------------------------------------------------- アクティビティ（外に触る側。awsio を別スレッドで呼ぶ）
@activity.defn
async def get_anomaly(anomaly_id: str) -> dict:
    a = await asyncio.to_thread(awsio.read_anomaly, anomaly_id)
    if not a:
        raise RuntimeError(f"anomaly {anomaly_id} not found")
    return a


@activity.defn
async def investigate(anomaly: dict) -> dict:
    text = await asyncio.to_thread(awsio.ask_agent, rules.build_prompt(anomaly))
    parsed = rules.parse_agent_json(text)
    action, command = rules.normalize_action(parsed["action"])
    return {"cause": parsed["cause"], "action": action, "command": command,
            "reason": parsed["reason"], "agent_response": text[:4000]}


@activity.defn
async def put_proposal(anomaly: dict, finding: dict, wf_id: str) -> str:
    """修復案を pending で書く。同じ proposal_id が既にあれば上書きしない（人が決めた status を pending に戻さないため）。
    あったのがこのワークフロー自身の書き込み（書けたあとで応答が切れて再試行された）なら、それを使って進む"""
    now = int(time.time())
    aid = anomaly["anomaly_id"]
    first_seen = int(anomaly.get("first_seen") or 0)
    pid = rules.proposal_id(aid, first_seen)
    written = await asyncio.to_thread(awsio.write_proposal, {
        "proposal_id": pid, "anomaly_id": aid, "device_id": anomaly.get("device_id", ""),
        "kind": anomaly.get("kind", ""), "target": anomaly.get("target", ""),
        "first_seen": first_seen, "status": "pending",
        "cause": finding["cause"], "action": finding["action"], "command": finding["command"],
        "reason": finding["reason"], "agent_response": finding["agent_response"],
        "workflow_id": wf_id, "created_at": now, "updated_at": now,
    }, True)
    if not written:
        existing = await asyncio.to_thread(awsio.read_proposal, pid)
        if existing.get("workflow_id") != wf_id:
            raise ApplicationError(f"proposal {pid} は別のワークフロー（{existing.get('workflow_id', '-')}）が書いた", non_retryable=True)
    return pid


@activity.defn
async def get_decision(proposal_id: str) -> str:
    p = await asyncio.to_thread(awsio.read_proposal, proposal_id)
    return p.get("status", "pending")


@activity.defn
async def set_status(proposal_id: str, status: str, fields: dict | None = None) -> None:
    await asyncio.to_thread(awsio.update_proposal, proposal_id, {"status": status, **(fields or {})})


@activity.defn
async def apply_on_lab(command: str) -> dict:
    if not awsio.LAB_INSTANCE_ID:
        return {"status": "Skipped", "output": "LAB_INSTANCE_ID が無い（terraform/pipeline/lab が無い）"}
    status, out = await asyncio.to_thread(awsio.run_on_lab, command)
    return {"status": status, "output": out}


def _same_open(a: dict, first_seen: int) -> bool:
    return a.get("status") == "open" and int(a.get("first_seen") or 0) == int(first_seen)


@activity.defn
async def still_open(anomaly_id: str, first_seen: int) -> bool:
    """この発生（anomaly_id + first_seen）がまだ open か。承認のあいだに閉じた・開き直した異常へ古い処置を打たないために見る"""
    return _same_open(await asyncio.to_thread(awsio.read_anomaly, anomaly_id), first_seen)


@activity.defn
async def anomaly_resolved(anomaly_id: str, first_seen: int = 0) -> bool:
    """この発生が閉じたか。閉じたあとで開き直していれば first_seen が変わるので、それも「この発生は閉じた」に数える"""
    a = await asyncio.to_thread(awsio.read_anomaly, anomaly_id)
    if a.get("status") == "resolved":
        return True
    return bool(first_seen) and bool(a) and int(a.get("first_seen") or 0) != int(first_seen)


ACTIVITIES = [get_anomaly, investigate, put_proposal, get_decision, set_status, still_open, apply_on_lab, anomaly_resolved]


# ---------------------------------------------------------------- ワークフロー（決定的な側。AWS には触らない）
@workflow.defn
class InvestigateAnomaly:
    def __init__(self) -> None:
        self._decision = ""

    @workflow.signal
    def decide(self, decision: str) -> None:
        if decision in ("approved", "rejected"):
            self._decision = decision

    @workflow.run
    async def run(self, anomaly_id: str, first_seen: int = 0) -> str:
        opts = {"start_to_close_timeout": timedelta(seconds=60), "retry_policy": RETRY}
        anomaly = await workflow.execute_activity(get_anomaly, anomaly_id, **opts)
        first_seen = int(first_seen or anomaly.get("first_seen") or 0)
        # 起こしてから読むまでのあいだに閉じた・開き直した（別の発生になった）なら、調べる相手がもういない
        if not _same_open(anomaly, first_seen):
            workflow.logger.info("anomaly %s#%s: もう open でない（obsolete）", anomaly_id, first_seen)
            return "obsolete"
        # AgentCore の読み取り待ちは awsio が 150 秒まで延ばしている。1 回分が start_to_close に収まるよう 4 分
        finding = await workflow.execute_activity(
            investigate, anomaly, start_to_close_timeout=timedelta(minutes=4), retry_policy=RETRY)
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

        # 承認までに何時間もかかりうる。そのあいだに閉じた・開き直した異常へ古い処置を打たない
        if not await workflow.execute_activity(still_open, args=[anomaly_id, first_seen], **opts):
            workflow.logger.info("proposal %s: obsolete（承認のあいだに異常が閉じた）", pid)
            await workflow.execute_activity(
                set_status, args=[pid, "obsolete", {"verify_note": "承認のあいだにこの異常が閉じた（または開き直して別の発生になった）ので打たなかった"}], **opts)
            return "obsolete"

        # 承認された。none なら打つものが無いので applied 扱いで verify へ
        if finding["command"]:
            # 打つのは 1 回だけ（maximum_attempts=1）。SSM に届かない・時間切れはアクティビティの失敗（ActivityError）として返るので、
            # 握らないとワークフローごと失敗して proposal が approved のまま残る。握って failed を書く
            try:
                result = await workflow.execute_activity(
                    apply_on_lab, finding["command"], start_to_close_timeout=timedelta(minutes=4), retry_policy=RetryPolicy(maximum_attempts=1))
            except ActivityError as e:
                cause = e.cause or e
                result = {"status": "Error", "output": f"{type(cause).__name__}: {cause}"}
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
            if await workflow.execute_activity(anomaly_resolved, args=[anomaly_id, first_seen], **opts):
                workflow.logger.info("proposal %s: verified (%d)", pid, i + 1)
                await workflow.execute_activity(
                    set_status, args=[pid, "verified", {"verify_note": f"{i + 1} 回目の確認で resolved"}], **opts)
                return "verified"
        workflow.logger.info("proposal %s: still open after %d checks", pid, VERIFY_ATTEMPTS)
        await workflow.execute_activity(
            set_status, args=[pid, "failed", {"verify_note": f"{VERIFY_ATTEMPTS} 回確かめても open のまま"}], **opts)
        return "failed"


# ---------------------------------------------------------------- starter（異常を拾ってワークフローを起こす）
async def start_for(client: Client, anomaly: dict) -> bool:
    """1 つの異常（の今の発生）についてワークフローを起こす（起こさない理由があれば False）。
    WorkflowAlreadyStartedError（同じ発生のワークフローが走っている）は False。それ以外の失敗は呼び手に投げる"""
    aid, first_seen = anomaly.get("anomaly_id", ""), int(anomaly.get("first_seen") or 0)
    existing = await asyncio.to_thread(awsio.read_proposal, rules.proposal_id(aid, first_seen)) if aid else {}
    if not rules.should_start(anomaly, existing):
        return False
    wid = rules.workflow_id(aid, first_seen)
    try:
        await client.start_workflow(InvestigateAnomaly.run, args=[aid, first_seen], id=wid, task_queue=TASK_QUEUE)
        log.info("started %s", wid)
        return True
    except WorkflowAlreadyStartedError:
        return False


async def handle_message(client: Client, body: str) -> None:
    """SQS のメッセージ 1 通。返れば消してよい、例外なら消さない（配り直させる）"""
    aid, first_seen = rules.event_from_message(body)
    if not aid:
        log.warning("starter: 読めないメッセージ（消す）: %s", str(body)[:200])
        return
    a = await asyncio.to_thread(awsio.read_anomaly, aid)
    if not a:
        log.warning("starter: anomaly %s がテーブルに無い（メッセージは消す）", aid)
        return
    # テーブルは状態の確かめにだけ使う。イベントの発生（first_seen）と今の行が違えば、その発生はもう閉じている
    if first_seen and int(a.get("first_seen") or 0) != first_seen:
        log.info("starter: %s#%s はもう別の発生になっている（消す）", aid, first_seen)
        return
    await start_for(client, a)


async def starter_queue(client: Client) -> None:
    """SQS（EventBridge のルールが流す AnomalyOpened）を待つ。1 通ずつ起こして消す。
    WorkflowAlreadyStartedError は start_for が False にする（同じ発生の重複配達で、その発生は既に走っているので消してよい。
    残すと可視性タイムアウトごとに配り直され、5 回で DLQ に落ちて「処理できなかった」ものと見分けがつかなくなる）。
    Temporal や DynamoDB に届かないなどの失敗は消さずに残す（配り直し、直らなければ DLQ）"""
    for m in await asyncio.to_thread(awsio.receive_messages):
        try:
            await handle_message(client, m.get("Body", ""))
        except Exception as e:  # noqa: BLE001 - 消さずに次へ（可視性タイムアウトのあとで配り直される）
            log.warning("starter: %s: %s（メッセージは残す）", type(e).__name__, str(e)[:300])
            continue
        await asyncio.to_thread(awsio.delete_message, m["ReceiptHandle"])


async def starter_table(client: Client) -> None:
    """キューが無いとき（terraform/workflow の events.tf を配備していない）は open の一覧を polling"""
    for a in await asyncio.to_thread(awsio.list_open_anomalies):
        await start_for(client, a)
    await asyncio.sleep(POLL_INTERVAL)


async def starter(client: Client) -> None:
    once = starter_queue if awsio.ANOMALY_QUEUE_URL else starter_table
    while True:
        try:
            await once(client)
        except (WorkflowFailureError, RuntimeError, OSError) as e:
            log.warning("starter: %s", str(e)[:300])
            await asyncio.sleep(5)
        except Exception as e:  # noqa: BLE001 - boto の例外は種類が多いので落とさずログに出す
            log.warning("starter: %s: %s", type(e).__name__, str(e)[:300])
            await asyncio.sleep(5)


# ---------------------------------------------------------------- 起動
async def connect() -> Client:
    for i in range(60):
        try:
            return await Client.connect(TEMPORAL_ADDRESS)
        except Exception as e:  # noqa: BLE001 - temporal が起きるまで待つ
            log.info("waiting for temporal (%s): %s", TEMPORAL_ADDRESS, str(e)[:100])
            await asyncio.sleep(5)
    raise RuntimeError("temporal did not come up")


async def main() -> None:
    for k in ("ANOMALY_TABLE", "PROPOSAL_TABLE", "AGENT_RUNTIME_ARN"):
        if not getattr(awsio, k):
            raise SystemExit(f"{k} が無い")
    client = await connect()
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[InvestigateAnomaly], activities=ACTIVITIES)
    log.info("worker up: queue=%s source=%s poll=%ss approval_timeout=%smin lab=%s", TASK_QUEUE,
             "sqs" if awsio.ANOMALY_QUEUE_URL else "table", POLL_INTERVAL, APPROVAL_TIMEOUT_MINUTES,
             awsio.LAB_INSTANCE_ID or "-")
    await asyncio.gather(worker.run(), starter(client))


if __name__ == "__main__":
    asyncio.run(main())
