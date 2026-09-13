"""AgentCore Runtime に載せる最小のチャットエージェント（フェーズ 1 相当）。

HTTP の口は bedrock-agentcore SDK が持つ: 0.0.0.0:8080 の POST /invocations と GET /ping。
AgentCore Runtime は 1 セッション = 1 microVM なので、モジュール変数の会話履歴はセッションごとに分かれる。

入力: {"prompt": "..."}
出力: {"status": "success", "response": "..."} か {"status": "error", "message": "..."}
"""

import logging
import os

import boto3
from bedrock_agentcore import BedrockAgentCoreApp
from botocore.exceptions import BotoCoreError, ClientError

MODEL_ID = os.environ["MODEL_ID"]
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "ap-northeast-1")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "1024"))
MAX_TURNS = int(os.environ.get("MAX_TURNS", "10"))
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "あなたはネットワーク運用を手伝うアシスタントです。日本語で簡潔に答えてください。",
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent")

bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
app = BedrockAgentCoreApp()
history: list[dict] = []


@app.entrypoint
def invoke(payload):
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return {"status": "error", "message": "prompt は空でない文字列で送る"}

    history.append({"role": "user", "content": [{"text": prompt}]})
    try:
        res = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            # 履歴は常に user で始まり user で終わる奇数長。直近 MAX_TURNS 往復だけ送る
            messages=history[-(2 * MAX_TURNS + 1):],
            inferenceConfig={"maxTokens": MAX_TOKENS},
        )
    except (ClientError, BotoCoreError):
        history.pop()
        log.exception("converse failed")
        return {"status": "error", "message": "モデルの呼び出しに失敗した"}

    message = res["output"]["message"]
    history.append(message)
    text = "".join(block.get("text", "") for block in message.get("content", []))
    usage = res.get("usage", {})
    log.info("tokens in=%s out=%s", usage.get("inputTokens"), usage.get("outputTokens"))
    return {"status": "success", "response": text}


if __name__ == "__main__":
    # host を明示する（省略すると SDK がコンテナ判定に失敗したとき 127.0.0.1 で待ち受ける）
    app.run(host="0.0.0.0", port=8080)
