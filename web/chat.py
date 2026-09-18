"""「チャット」タブの中身。質問を AgentCore Runtime に送って答えを返す。

セッション ID はブラウザのセッションごとに 1 つ（Runtime 側の会話履歴はこの ID で分かれる）。
Runtime の ARN は環境変数 RUNTIME_ARN があればそれ、無ければ SSM の <PARAM_PREFIX>/runtime-arn
（terraform/agent が書く）を 60 秒ごとに読む。どちらも無ければ agent が配備されていない
（チャットだけ使えない。トポロジ・異常・承認のタブは動く）。
"""

import json
import uuid

import boto3
import gradio as gr
from botocore.exceptions import BotoCoreError, ClientError

from config import MAX_PROMPT, REGION, log

import toolkit  # config が sys.path を通したあとで読む（agent/toolkit.py）

RUNTIME_ARN = toolkit.Param("RUNTIME_ARN", "runtime-arn")

# 読み取りのタイムアウトは Runtime の応答（ツール往復を含む）より長くする
agentcore = boto3.client("bedrock-agentcore", region_name=REGION,
                         config=boto3.session.Config(read_timeout=150, connect_timeout=10, retries={"max_attempts": 1}))


def invoke(prompt: str, session_id: str) -> str:
    arn = RUNTIME_ARN.value()
    if not arn:
        raise gr.Error("エージェントが配備されていません（terraform/agent を apply する。deploy.env の AGENT=1）")
    try:
        res = agentcore.invoke_agent_runtime(
            agentRuntimeArn=arn, runtimeSessionId=session_id, qualifier="DEFAULT",
            contentType="application/json", accept="application/json",
            payload=json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8"),
        )
        data = json.loads(res["response"].read())
    except (ClientError, BotoCoreError) as e:
        log.error("invoke failed: %s", str(e)[:500])
        raise gr.Error("エージェントの呼び出しに失敗しました")
    except ValueError:
        log.exception("unreadable agent response")
        raise gr.Error("エージェントの応答を読めませんでした")
    if not isinstance(data, dict) or data.get("status") != "success":
        log.error("agent error: %s", str(data)[:500])
        raise gr.Error("エージェントがエラーを返しました")
    return data.get("response", "")


def respond(message: str, chat: list, session_id: str):
    message = (message or "").strip()
    if not message:
        return "", chat, session_id
    if len(message) > MAX_PROMPT:
        raise gr.Error(f"質問は {MAX_PROMPT} 文字まで")
    if not session_id:
        # 36 文字。runtimeSessionId は 33 文字以上
        session_id = str(uuid.uuid4())
    chat = chat + [{"role": "user", "content": message}]
    chat = chat + [{"role": "assistant", "content": invoke(message, session_id)}]
    return "", chat, session_id


def new_session(_chat, _session_id):
    return [], ""
