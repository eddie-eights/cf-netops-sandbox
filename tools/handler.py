"""AgentCore Gateway（MCP）の裏で動く tools Lambda（terraform/workflow）。

Gateway は MCP の tools/call を Lambda の同期呼び出しに変える。event がツールの引数そのもので、
ツール名は context.client_context.custom["bedrockAgentCoreToolName"] に "<ターゲット名>___<ツール名>" の形で入る。
中身は agent/topology.py と agent/anomalies.py（zip に同梱。Terraform の archive_file が集める）を呼ぶだけ。
Neptune は VPC の中で、この Lambda は外にいるので、トポロジは data/ の静的データ（PARAM_PREFIX を渡さない）。
"""

import json
import logging

import anomalies
import topology

log = logging.getLogger()
log.setLevel(logging.INFO)

TOOL_NAME_KEY = "bedrockAgentCoreToolName"
DELIMITER = "___"


def tool_name(context) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    raw = custom.get(TOOL_NAME_KEY, "") if isinstance(custom, dict) else ""
    return raw.rsplit(DELIMITER, 1)[-1]


def dispatch(name: str, args: dict) -> dict:
    if name in topology.TOOLS:
        topology.reload()
        return topology.run_tool(name, args)
    if name in anomalies.TOOLS:
        return anomalies.run_tool(name, args)
    return {"error": f"unknown tool {name}"}


def handler(event, context):
    name = tool_name(context)
    args = event if isinstance(event, dict) else {}
    log.info("tool=%s args=%s", name, json.dumps(args, ensure_ascii=False)[:500])
    return dispatch(name, args)
