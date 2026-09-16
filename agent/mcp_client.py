"""AgentCore Gateway（MCP）のクライアント。Runtime から Gateway のツールを Converse の toolSpec として使う。

Gateway の URL は環境変数 GATEWAY_URL、無ければ SSM の <PARAM_PREFIX>/gateway-url（terraform/workflow が書く）。
どちらも無ければ空を返し、app.py はコンテナ内の関数（topology.py / anomalies.py）を使う。
Gateway の認可は AWS_IAM なので、リクエストを SigV4（サービス名 bedrock-agentcore）で署名して JSON-RPC を POST する。
Gateway のツール名は "<ターゲット名>___<ツール名>" なので、モデルには後ろの <ツール名> だけを見せ、呼ぶときに戻す。
tools/list の結果は TTL 秒だけ持つ（質問のたびに Gateway を叩かない）。
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError, ClientError

PARAM_PREFIX = os.environ.get("PARAM_PREFIX", "")
REGION = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION") or "ap-northeast-1"
TTL = int(os.environ.get("GATEWAY_TTL", "300"))
TIMEOUT = int(os.environ.get("GATEWAY_TIMEOUT", "30"))
DELIMITER = "___"
PROTOCOL_VERSION = "2025-06-18"
log = logging.getLogger("mcp")
_cache = {"url": "", "checked": 0.0, "specs": [], "names": {}, "listed": 0.0}
_ids = {"n": 0}


def gateway_url() -> str:
    env = os.environ.get("GATEWAY_URL", "")
    if env:
        return env
    if _cache["url"] or time.time() - _cache["checked"] < TTL or not PARAM_PREFIX:
        return _cache["url"]
    _cache["checked"] = time.time()
    try:
        _cache["url"] = boto3.client("ssm", region_name=REGION).get_parameter(
            Name=f"{PARAM_PREFIX}/gateway-url")["Parameter"]["Value"]
    except (ClientError, BotoCoreError):
        _cache["url"] = ""
    return _cache["url"]


def parse_response(content_type: str, text: str) -> dict:
    """JSON か SSE（data: 行に JSON-RPC の応答）。SSE は result / error を持つ最後の行を採る"""
    if "text/event-stream" not in (content_type or ""):
        return json.loads(text) if text.strip() else {}
    last: dict = {}
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            msg = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            last = msg
    return last


def _post(url: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    req = AWSRequest(method="POST", url=url, data=data, headers=headers)
    creds = boto3.Session(region_name=REGION).get_credentials()
    if creds is None:
        raise RuntimeError("no AWS credentials for the gateway request")
    SigV4Auth(creds.get_frozen_credentials(), "bedrock-agentcore", REGION).add_auth(req)
    http = urllib.request.Request(url, data=data, headers=dict(req.headers.items()), method="POST")
    with urllib.request.urlopen(http, timeout=TIMEOUT) as resp:
        return parse_response(resp.headers.get("Content-Type", ""), resp.read().decode("utf-8"))


def _rpc(url: str, method: str, params: dict) -> dict:
    _ids["n"] += 1
    res = _post(url, {"jsonrpc": "2.0", "id": _ids["n"], "method": method, "params": params})
    if "error" in res:
        raise RuntimeError(f"gateway {method}: {json.dumps(res['error'], ensure_ascii=False)[:300]}")
    return res.get("result") or {}


def to_tool_specs(tools: list) -> tuple[list, dict]:
    """MCP の tools/list → Converse の toolSpec と、短い名前 → Gateway の名前 の対応"""
    specs, names = [], {}
    for t in tools or []:
        full = t.get("name", "")
        if not full:
            continue
        short = full.rsplit(DELIMITER, 1)[-1]
        names[short] = full
        schema = t.get("inputSchema") or {"type": "object", "properties": {}}
        specs.append({"toolSpec": {
            "name": short,
            "description": (t.get("description") or short)[:1024],
            "inputSchema": {"json": schema},
        }})
    return specs, names


def tool_specs() -> list:
    """Gateway のツール一覧を toolSpec で。Gateway が無い・届かないときは []（app.py がコンテナ内の関数に戻す）"""
    url = gateway_url()
    if not url:
        return []
    if _cache["listed"] and time.time() - _cache["listed"] < TTL:
        return _cache["specs"]
    _cache["listed"] = time.time()
    try:
        result = _rpc(url, "tools/list", {})
        _cache["specs"], _cache["names"] = to_tool_specs(result.get("tools", []))
    except (OSError, ValueError, RuntimeError, BotoCoreError, ClientError) as e:
        log.warning("gateway tools/list failed, using local tools: %s", str(e)[:200])
        _cache["specs"], _cache["names"] = [], {}
    return _cache["specs"]


def has(name: str) -> bool:
    return name in _cache["names"]


def call(name: str, args: dict) -> dict:
    """tools/call。content のテキストが JSON ならそれを、違えば {"text": ...} を返す。失敗は {"error": ...}"""
    url = gateway_url()
    if not url or not has(name):
        return {"error": f"gateway tool {name} is not available"}
    try:
        result = _rpc(url, "tools/call", {"name": _cache["names"][name], "arguments": args or {}})
    except (OSError, ValueError, RuntimeError, BotoCoreError, ClientError) as e:
        log.warning("gateway tools/call %s failed: %s", name, str(e)[:200])
        return {"error": f"gateway call failed: {str(e)[:200]}"}
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError"):
        return {"error": text[:1000] or "tool error"}
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else {"result": out}
    except ValueError:
        return {"text": text}
