"""WORKFLOW のワーカー（Temporal on ECS Fargate）。
Grafana と Splunk のアラート（SNS → SQS）を受けてエージェントに原因を調べさせ、修復案を出し、人が承認したら lab EC2 で直し、解消の通知が来るまで確かめる。

流れ: Grafana のアラートルール / Splunk の保存済みサーチが異常を見つける → SNS のトピック（<接頭辞>-alerts）→ SQS → ここ（Temporal のワークフロー）が
エージェントに Neptune / S3 / OpenSearch / Prometheus を見させて原因分析 → 修復の提案 → 人間の承認 → Temporal で実行。
同じタスクの中の temporal コンテナ（temporal server start-dev、SQLite）に localhost:7233 でつなぐ。
1 プロセスで 2 つを動かす:
  - starter: SQS（ANOMALY_QUEUE_URL）を long polling（20 秒）し、届いたアラート（rules.alerts_from_message）ごとに
      firing   → investigate-<anomaly_id> のワークフローを起こす（起こすのは rules.START_KINDS（link_down）だけ）。
                 同じ id が走っていれば Temporal が二重起動を弾く（Splunk と Grafana が同じ障害を知らせても 1 つ。繰り返しの通知も同じ）
      resolved → そのワークフローにシグナル resolved を送る（走っていなければ何もしない）
    メッセージは、起こした・起こす理由が無い・もう起きている（WorkflowAlreadyStartedError）・送る相手がいない、のどれかなら消し、
    それ以外の失敗（Temporal や Neptune に届かない）なら消さずに残して、可視性タイムアウトのあとで配り直させる
  - worker: ワークフロー InvestigateAnomaly とアクティビティを回す

異常の「いま」を置く場所は持たない（2026-10-02。Neptune はトポロジだけにする。履歴の置き場は未定）。発生はワークフローそのもの、解消はシグナルで持つ。
ワークフローの段: investigate（AgentCore Runtime に JSON で答えさせる）
  → put_proposal（pending。proposal_id = <anomaly_id>#<first_seen>、同じ id が既にあれば上書きしない）
  → 人の判断を Neptune の修復案の頂点で待つ（web の「承認」タブが status を approved / rejected に変える。シグナル decide でも通る）。
    待つあいだに解消の通知が来たら、打つ相手がもういないので obsolete にして終わる
  → record_decision（シグナルで決まったなら頂点にも書く。承認・却下を証跡に残す）
  → approved なら、打つ直前にもう一度、解消していないか確かめる（していれば obsolete にして打たない）
  → apply_on_lab（SSM Run Command で `sudo lab <cmd>`。cmd は rules.ALLOWED_ACTIONS だけ）→ applied / failed
  → verify（解消の通知を VERIFY_TIMEOUT 秒まで待つ）→ verified / failed
  APPROVAL_TIMEOUT_MINUTES 過ぎたら expired。rejected なら何もしない。
  rejected / expired / failed で終わるときは、解消の通知が来るまで（長くて HOLD_MINUTES 分）ワークフローを閉じない。
  閉じると、まだ直っていない同じ異常の次の通知（Grafana の repeat、Splunk の次のサーチ）がもう一度調査を起こす。
修復案の「いま」は全部 Neptune の頂点（label proposal、id = proposal_id）に書くので、web は Temporal を知らなくてよい。
status が変わるたびに S3 Tables の proposal_events に 1 行足す（created / approved / rejected / expired / obsolete / applied / failed / verified。
rules.proposal_event）。Temporal の dev server は SQLite をタスクの中に持つだけで、タスクが入れ替わると履歴ごと消えるので、証跡はこちらに残す。
証跡は「頂点を書いてから 1 行足す」の順で、足す前に落ちたらアクティビティの再試行で足し直す（二重に入ったら event_id で落とす）。

3 ファイルに分けてある（同じディレクトリに置いて import する。Dockerfile は workflow/*.py を全部入れる）:
  rules.py   判断だけの純粋関数（プロンプト・JSON の読み取り・許可コマンド・アラートの読み取り・起こすかどうか）
  awsio.py   環境変数と AWS 呼び出し（Neptune / S3 Tables / AgentCore / SSM / SQS）
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
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

# Temporal はワークフローを決定的に保つため、@workflow.defn のあるこのモジュールをサンドボックスの中で再 import する。
# 自作モジュールはそのとき素通しにする（公式の作法）。素通しにしないと awsio / rules がサンドボックス用に作り直され、
# 同じ名前の別物になる
with workflow.unsafe.imports_passed_through():
    import awsio
    import rules

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TASK_QUEUE = os.environ.get("TASK_QUEUE", "nwc-investigate")
APPROVAL_TIMEOUT_MINUTES = int(os.environ.get("APPROVAL_TIMEOUT_MINUTES", "120"))
# 処置のあと、解消の通知を待つ秒数。通知は「機器 → Telegraf → MSK → Spark → AMP / Splunk → ルールの評価 → SNS → SQS」を通るので分の単位で遅れる
VERIFY_TIMEOUT = int(os.environ.get("VERIFY_TIMEOUT", "300"))
DECISION_POLL = int(os.environ.get("DECISION_POLL", "30"))
# 直らないまま終わった（rejected / expired / failed）あと、解消の通知を待ってワークフローの id を握っておく長さ（分）。過ぎたら閉じ、次の通知でもう一度調べる
HOLD_MINUTES = int(os.environ.get("HOLD_MINUTES", "1440"))
HOLD_OUTCOMES = ("rejected", "expired", "failed")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")

RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))


async def _audit(event: str, proposal: dict, detail: str = "", decided_by: str = "") -> None:
    """修復案の証跡（S3 Tables の proposal_events）に 1 行足す"""
    row = rules.proposal_event(event, proposal, int(time.time()), detail, decided_by)
    await asyncio.to_thread(awsio.append_proposal_events, [row], rules.PROPOSAL_EVENT_COLUMNS)


# ---------------------------------------------------------------- アクティビティ（外に触る側。awsio を別スレッドで呼ぶ）
@activity.defn
async def investigate(anomaly: dict) -> dict:
    text = await asyncio.to_thread(awsio.ask_agent, rules.build_prompt(anomaly))
    parsed = rules.parse_agent_json(text)
    action, command = rules.normalize_action(parsed["action"])
    return {"cause": parsed["cause"], "action": action, "command": command,
            "reason": parsed["reason"], "agent_response": text[:4000]}


@activity.defn
async def put_proposal(anomaly: dict, finding: dict, wf_id: str, run_id: str = "") -> str:
    """修復案を pending で書く。同じ proposal_id が既にあれば上書きしない（人が決めた status を pending に戻さないため）。
    あったのがこの実行自身の書き込み（書けたあとで応答が切れて再試行された）なら、それを使って進む。
    ワークフローの id は異常ごとで、同じ id の別の実行（前の発生）がありうるので、実行の id（run_id）まで比べる"""
    now = int(time.time())
    aid = anomaly["anomaly_id"]
    first_seen = int(anomaly.get("first_seen") or 0)
    pid = rules.proposal_id(aid, first_seen)
    item = {
        "proposal_id": pid, "anomaly_id": aid, "device_id": anomaly.get("device_id", ""),
        "kind": anomaly.get("kind", ""), "target": anomaly.get("target", ""),
        "first_seen": first_seen, "status": "pending",
        "cause": finding["cause"], "action": finding["action"], "command": finding["command"],
        "reason": finding["reason"], "agent_response": finding["agent_response"],
        "source": anomaly.get("source", ""), "detail": anomaly.get("detail", ""),
        "workflow_id": wf_id, "run_id": run_id, "created_at": now, "updated_at": now,
    }
    written = await asyncio.to_thread(awsio.write_proposal, item, True)
    if not written:
        existing = await asyncio.to_thread(awsio.read_proposal, pid)
        if existing.get("workflow_id") != wf_id or str(existing.get("run_id") or "") != run_id:
            raise ApplicationError(
                f"proposal {pid} は別の実行（{existing.get('workflow_id', '-')} / {existing.get('run_id') or '-'}）が書いた", non_retryable=True)
    # 書けたあとで落ちて再試行されたときも足す（証跡が抜けるより、同じ event_id が二重に入るほうがよい）
    await _audit("created", item, finding["reason"])
    return pid


@activity.defn
async def get_decision(proposal_id: str) -> str:
    p = await asyncio.to_thread(awsio.read_proposal, proposal_id)
    return p.get("status", "pending")


@activity.defn
async def record_decision(proposal_id: str, decision: str, via_signal: bool = False) -> str:
    """人の判断を確定して証跡に残し、効いた判断（approved / rejected）を返す。
    シグナル decide で決まったときは頂点にも書く（pending のときだけ。web が先に決めていればそちらが効く）"""
    if via_signal:
        now = int(time.time())
        await asyncio.to_thread(awsio.update_proposal, proposal_id,
                                {"status": decision, "decided_by": "temporal-signal", "decided_at": now}, "pending")
    p = await asyncio.to_thread(awsio.read_proposal, proposal_id)
    effective = p.get("status") if p.get("status") in ("approved", "rejected") else decision
    await _audit(effective, {**p, "proposal_id": proposal_id}, decided_by=str(p.get("decided_by") or ""))
    return effective


@activity.defn
async def set_status(proposal_id: str, status: str, fields: dict | None = None) -> None:
    fields = fields or {}
    await asyncio.to_thread(awsio.update_proposal, proposal_id, {"status": status, **fields})
    p = await asyncio.to_thread(awsio.read_proposal, proposal_id)
    await _audit(status, {**p, "proposal_id": proposal_id}, fields.get("apply_output") or fields.get("verify_note") or "")


@activity.defn
async def apply_on_lab(command: str) -> dict:
    if not awsio.LAB_INSTANCE_ID:
        return {"status": "Skipped", "output": "LAB_INSTANCE_ID が無い（terraform/pipeline/lab が無い）"}
    status, out = await asyncio.to_thread(awsio.run_on_lab, command)
    return {"status": status, "output": out}


ACTIVITIES = [investigate, put_proposal, get_decision, record_decision, set_status, apply_on_lab]


# ---------------------------------------------------------------- ワークフロー（決定的な側。AWS には触らない）
@workflow.defn
class InvestigateAnomaly:
    def __init__(self) -> None:
        self._decision = ""
        self._resolved = False

    @workflow.signal
    def decide(self, decision: str) -> None:
        if decision in ("approved", "rejected"):
            self._decision = decision

    @workflow.signal
    def resolved(self, source: str = "") -> None:
        """解消の通知（starter が SQS の resolved を受けて送る）"""
        self._resolved = True

    async def _wait_resolved(self, seconds: int) -> bool:
        # wait_condition は timeout に達すると asyncio.TimeoutError を投げ、漏らすとワークフロー自体が失敗する（下の承認待ちと同じ）
        try:
            await workflow.wait_condition(lambda: self._resolved, timeout=timedelta(seconds=seconds))
        except asyncio.TimeoutError:
            pass
        return self._resolved

    @workflow.run
    async def run(self, anomaly: dict) -> str:
        outcome = await self._handle(anomaly)
        # 直っていないまま閉じると、同じ異常の次の通知（Grafana の repeat、Splunk の次のサーチ）がもう一度調査を起こす。
        # 解消の通知が来るまで id を握っておく（長くて HOLD_MINUTES 分。過ぎたら閉じて、次の通知で調べ直す）
        if outcome in HOLD_OUTCOMES and not self._resolved:
            workflow.logger.info("anomaly %s: %s。解消の通知を待つ（%d 分まで）", anomaly.get("anomaly_id"), outcome, HOLD_MINUTES)
            await self._wait_resolved(HOLD_MINUTES * 60)
        return outcome

    async def _handle(self, anomaly: dict) -> str:
        opts = {"start_to_close_timeout": timedelta(seconds=60), "retry_policy": RETRY}
        # AgentCore の読み取り待ちは awsio が 150 秒まで延ばしている。1 回分が start_to_close に収まるよう 4 分
        finding = await workflow.execute_activity(
            investigate, anomaly, start_to_close_timeout=timedelta(minutes=4), retry_policy=RETRY)
        info = workflow.info()
        pid = await workflow.execute_activity(put_proposal, args=[anomaly, finding, info.workflow_id, info.run_id], **opts)

        workflow.logger.info("proposal %s: pending (action=%s)", pid, finding["action"])

        # 人の判断を待つ（頂点の status か、シグナル decide）。待つあいだに解消したら、打つ相手がもういない
        deadline = workflow.now() + timedelta(minutes=APPROVAL_TIMEOUT_MINUTES)
        decision, via_signal = "", False
        while workflow.now() < deadline and not self._resolved:
            # wait_condition は timeout に達すると asyncio.TimeoutError を投げ、それをそのまま漏らすと
            # ワークフロー自体が失敗する（temporalio は TimeoutError をタスク失敗でなくワークフロー失敗にする。
            # 2026-09-18 に承認しても applied に進まない原因だった）。時間切れは「まだ決まっていない」なので握って頂点を見る
            try:
                await workflow.wait_condition(lambda: bool(self._decision) or self._resolved, timeout=timedelta(seconds=DECISION_POLL))
            except asyncio.TimeoutError:
                pass
            if self._decision:
                decision, via_signal = self._decision, True
                break
            status = await workflow.execute_activity(get_decision, pid, **opts)
            if status in ("approved", "rejected"):
                decision = status
                break
        if not decision:
            if self._resolved:
                workflow.logger.info("proposal %s: obsolete（承認を待つあいだに解消した）", pid)
                await workflow.execute_activity(
                    set_status, args=[pid, "obsolete", {"verify_note": "承認を待つあいだにこの異常が解消したので打たなかった"}], **opts)
                return "obsolete"
            workflow.logger.info("proposal %s: expired", pid)
            await workflow.execute_activity(set_status, args=[pid, "expired", {"verify_note": "承認待ちのまま時間切れ"}], **opts)
            return "expired"
        decision = await workflow.execute_activity(record_decision, args=[pid, decision, via_signal], **opts)
        workflow.logger.info("proposal %s: %s", pid, decision)
        if decision == "rejected":
            return "rejected"

        # 承認と同時に解消の通知が届いていることがある。解消した異常へ古い処置を打たない
        if self._resolved:
            workflow.logger.info("proposal %s: obsolete（承認のあいだに異常が解消した）", pid)
            await workflow.execute_activity(
                set_status, args=[pid, "obsolete", {"verify_note": "承認のあいだにこの異常が解消したので打たなかった"}], **opts)
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

        applied_at = workflow.now()
        if await self._wait_resolved(VERIFY_TIMEOUT):
            waited = int((workflow.now() - applied_at).total_seconds())
            workflow.logger.info("proposal %s: verified (%ds)", pid, waited)
            await workflow.execute_activity(
                set_status, args=[pid, "verified", {"verify_note": f"処置から {waited} 秒で解消の通知が届いた"}], **opts)
            return "verified"
        workflow.logger.info("proposal %s: not resolved after %ds", pid, VERIFY_TIMEOUT)
        await workflow.execute_activity(
            set_status, args=[pid, "failed", {"verify_note": f"{VERIFY_TIMEOUT} 秒待っても解消の通知が届かない"}], **opts)
        return "failed"


# ---------------------------------------------------------------- starter（アラートを拾ってワークフローを起こす・解消を伝える）
async def start_for(client: Client, alert: dict) -> bool:
    """firing のアラート 1 件についてワークフローを起こす（起こさない理由があれば False）。
    WorkflowAlreadyStartedError（同じ異常のワークフローが走っている）は False。それ以外の失敗は呼び手に投げる"""
    aid = alert.get("anomaly_id", "")
    if not rules.should_start(alert, None):
        return False
    # 同じ発生の修復案がもうあれば起こさない（ワークフローが閉じたあとで届いた、同じ starts_at の繰り返しの通知）
    existing = await asyncio.to_thread(awsio.read_proposal, rules.proposal_id(aid, alert.get("first_seen")))
    if not rules.should_start(alert, existing):
        return False
    wid = rules.workflow_id(aid)
    try:
        await client.start_workflow(InvestigateAnomaly.run, alert, id=wid, task_queue=TASK_QUEUE)
        log.info("started %s (%s)", wid, alert.get("source") or "-")
        return True
    except WorkflowAlreadyStartedError:
        return False


async def resolve_for(client: Client, alert: dict) -> bool:
    """resolved のアラート 1 件を、走っているワークフローにシグナルで伝える。走っていなければ（NOT_FOUND）False"""
    wid = rules.workflow_id(alert.get("anomaly_id", ""))
    try:
        await client.get_workflow_handle(wid).signal(InvestigateAnomaly.resolved, alert.get("source", ""))
        log.info("resolved %s (%s)", wid, alert.get("source") or "-")
        return True
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            return False
        raise


async def handle_message(client: Client, body: str) -> None:
    """SQS のメッセージ 1 通（アラートが 1 件以上）。返れば消してよい、例外なら消さない（配り直させる。どちらの処理も繰り返して害が無い）"""
    alerts = rules.alerts_from_message(body, int(time.time()))
    if not alerts:
        log.warning("starter: 読めないメッセージ（消す）: %s", str(body)[:200])
        return
    for a in alerts:
        if a["kind"] not in rules.START_KINDS:
            continue
        if a["status"] == "resolved":
            await resolve_for(client, a)
        else:
            await start_for(client, a)


async def starter_queue(client: Client) -> None:
    """SQS（SNS のトピックの購読）を待つ。1 通ずつ処理して消す。
    WorkflowAlreadyStartedError は start_for が False にする（同じ異常の重複配達で、もう走っているので消してよい。
    残すと可視性タイムアウトごとに配り直され、5 回で DLQ に落ちて「処理できなかった」ものと見分けがつかなくなる）。
    Temporal や Neptune に届かないなどの失敗は消さずに残す（配り直し、直らなければ DLQ）"""
    for m in await asyncio.to_thread(awsio.receive_messages):
        try:
            await handle_message(client, m.get("Body", ""))
        except Exception as e:  # noqa: BLE001 - 消さずに次へ（可視性タイムアウトのあとで配り直される）
            log.warning("starter: %s: %s（メッセージは残す）", type(e).__name__, str(e)[:300])
            continue
        await asyncio.to_thread(awsio.delete_message, m["ReceiptHandle"])


async def starter(client: Client) -> None:
    while True:
        try:
            await starter_queue(client)
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
    for k in ("ANOMALY_QUEUE_URL", "NEPTUNE_ENDPOINT", "AUDIT_TABLE_BUCKET_ARN", "AUDIT_NAMESPACE", "AGENT_RUNTIME_ARN"):
        if not getattr(awsio, k):
            raise SystemExit(f"{k} が無い")
    client = await connect()
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[InvestigateAnomaly], activities=ACTIVITIES)
    log.info("worker up: queue=%s approval_timeout=%smin verify_timeout=%ss hold=%smin lab=%s", TASK_QUEUE,
             APPROVAL_TIMEOUT_MINUTES, VERIFY_TIMEOUT, HOLD_MINUTES, awsio.LAB_INSTANCE_ID or "-")
    await asyncio.gather(worker.run(), starter(client))


if __name__ == "__main__":
    asyncio.run(main())
