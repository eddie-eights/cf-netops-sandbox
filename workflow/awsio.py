"""ワーカーの「外に触る」部分。環境変数の読み出しと AWS 呼び出しをここにまとめる。

worker.py のアクティビティは全部このファイルの関数を asyncio.to_thread で呼ぶ。
Temporal のワークフロー（決定的でないといけない）から直接呼ぶものは 1 つも無い。

  DynamoDB   anomalies（異常の現在）と proposals（修復案の現在）の読み書き
  AgentCore  Runtime を invoke して原因分析を答えさせる
  SSM        lab EC2 に Run Command で 1 行打つ
  SQS        Spark の検知（EventBridge → SQS）を long polling で受け取る

boto3 は import せず、呼ばれたときに _boto() の中で読む。Temporal のワークフローサンドボックスが
このモジュールを再 import するときに重い依存を引きずらないようにするため。
"""

import json
import os
import time
import uuid

ANOMALY_TABLE = os.environ.get("ANOMALY_TABLE", "")
ANOMALY_QUEUE_URL = os.environ.get("ANOMALY_QUEUE_URL", "")  # terraform/workflow の events.tf。空ならテーブルを polling
PROPOSAL_TABLE = os.environ.get("PROPOSAL_TABLE", "")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
LAB_INSTANCE_ID = os.environ.get("LAB_INSTANCE_ID", "")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")


def _boto(name):
    import boto3

    return boto3.client(name, region_name=REGION)


def _plain(item: dict) -> dict:
    """DynamoDB の型付き表現（{"S": "x"}）を素の dict にする"""
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
    """_plain の逆。空文字は DynamoDB のソートキーに使えないので "-" にする"""
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return {"S": str(value) if str(value) else "-"}


# ---------------------------------------------------------------- DynamoDB
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


# ---------------------------------------------------------------- AgentCore Runtime
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


# ---------------------------------------------------------------- SSM（lab EC2）
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


# ---------------------------------------------------------------- SQS（Spark の検知）
def receive_messages() -> list:
    return _boto("sqs").receive_message(QueueUrl=ANOMALY_QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=20).get("Messages", [])


def delete_message(receipt: str) -> None:
    _boto("sqs").delete_message(QueueUrl=ANOMALY_QUEUE_URL, ReceiptHandle=receipt)
