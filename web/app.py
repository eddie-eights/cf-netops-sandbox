"""チャット Web（Gradio）。127.0.0.1 だけで待ち受け、利用者は SSM のポートフォワーディングで開く。

タブは 2 つ:
  - チャット: 質問を AgentCore Runtime に送る（boto3 の invoke_agent_runtime。署名はインスタンスロール）。
    セッション ID はブラウザのセッションごとに 1 つ（Runtime 側の会話履歴はこの ID で分かれる）
  - トポロジ: data/topology.json を段（pe / ce / host）に分けて SVG に描き、data/devices.yaml を表で出す。
    エージェントのコンテナに入っているのと同じ静的データ。lab（lab.yaml）には触らない

依存（gradio / boto3 / pyyaml）は S3 に置いた wheel から入れる（main.yaml の UserData）。インターネットには出ない。
"""

import html
import json
import logging
import os
import uuid

import boto3
import gradio as gr
import pandas as pd
import yaml
from botocore.exceptions import BotoCoreError, ClientError

ARN = os.environ["RUNTIME_ARN"]
REGION = os.environ["AWS_REGION"]
PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
TITLE = os.environ.get("TITLE", "NWC PoC")
MAX_PROMPT = 4000

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("web")
# 読み取りのタイムアウトは Runtime の応答（ツール往復を含む）より長くする
agentcore = boto3.client("bedrock-agentcore", region_name=REGION,
                         config=boto3.session.Config(read_timeout=150, connect_timeout=10, retries={"max_attempts": 1}))


# ---------------------------------------------------------------- data
with open(os.path.join(DATA_DIR, "devices.yaml"), encoding="utf-8") as f:
    DEVICES = yaml.safe_load(f)["devices"]
with open(os.path.join(DATA_DIR, "topology.json"), encoding="utf-8") as f:
    TOPO = json.load(f)
NODES = {n["device_id"]: n for n in TOPO["nodes"]}
ROLE_ORDER = ["core", "pe", "distribution", "aggregation", "access", "ce", "host"]
ROLE_LABEL = {"pe": "キャリア PE", "ce": "拠点 CE", "host": "LAN 端末"}


def device_table() -> pd.DataFrame:
    degree = {}
    for l in TOPO["links"]:
        degree[l["a"]] = degree.get(l["a"], 0) + 1
        degree[l["b"]] = degree.get(l["b"], 0) + 1
    rows = []
    for d in DEVICES:
        n = NODES.get(d["device_id"], {})
        rows.append({
            "機器": d["device_id"], "拠点": d["site"], "役割": d["role"], "AS": n.get("asn") or "",
            "管理 IP": d["mgmt_ip"], "リンク数": degree.get(d["device_id"], 0),
            "監視": "対象" if d.get("enabled") else "対象外",
        })
    rows.sort(key=lambda r: (ROLE_ORDER.index(r["役割"]) if r["役割"] in ROLE_ORDER else 99, r["機器"]))
    return pd.DataFrame(rows)


def topology_svg() -> str:
    """段ごとに横へ並べた素朴な図。位置は device_id の順で決まるので、再読み込みしても動かない"""
    layers = {}
    for n in TOPO["nodes"]:
        layers.setdefault(n["role"], []).append(n["device_id"])
    roles = [r for r in ROLE_ORDER if r in layers]
    width, row_h, top, left, node_w, node_h = 860, 150, 50, 70, 132, 44
    pos = {}
    for i, role in enumerate(roles):
        ids = sorted(layers[role])
        step = (width - 2 * left) / max(len(ids), 1)
        for j, dev in enumerate(ids):
            pos[dev] = (left + step * (j + 0.5), top + row_h * i + 30)
    height = top + row_h * len(roles)
    color = {"ebgp": "#1f5fbf", "ibgp": "#7a4bd6", "l2": "#8a949e", "mgmt": "#c0c8d0"}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
           f'style="width:100%;max-width:{width}px;font-family:system-ui,sans-serif;font-size:12px">']
    for i, role in enumerate(roles):
        y = top + row_h * i + 30
        out.append(f'<text x="8" y="{y + 4}" fill="#6b7480" font-size="11">{html.escape(ROLE_LABEL.get(role, role))}</text>')
    for l in TOPO["links"]:
        if l["a"] not in pos or l["b"] not in pos:
            continue
        (x1, y1), (x2, y2) = pos[l["a"]], pos[l["b"]]
        dash = ' stroke-dasharray="6 4"' if l.get("role") == "secondary" else ""
        w = 3 if (l.get("bandwidth_mbps") or 0) >= 1000 else 1.6
        title = f'{l["a"]} {l["a_if"]} - {l["b"]} {l["b_if"]} ({l["kind"]}{" " + l["role"] if l.get("role") else ""}, {l.get("bandwidth_mbps")} Mbps)'
        out.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="{color.get(l["kind"], "#999")}" '
                   f'stroke-width="{w}"{dash}><title>{html.escape(title)}</title></line>')
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        out.append(f'<text x="{mx:.0f}" y="{my - 4:.0f}" text-anchor="middle" fill="#4b5563" font-size="10">'
                   f'{html.escape(l["a_if"] + "/" + l["b_if"])}</text>')
    for dev, (x, y) in pos.items():
        n = NODES[dev]
        fill = {"pe": "#e8f0fe", "ce": "#e6f4ea", "host": "#f3f4f6"}.get(n["role"], "#fff")
        asn = f'AS {n["asn"]}' if n.get("asn") else n["site"]
        out.append(f'<rect x="{x - node_w / 2:.0f}" y="{y - node_h / 2:.0f}" width="{node_w}" height="{node_h}" rx="6" '
                   f'fill="{fill}" stroke="#374151" stroke-width="1.2"/>')
        out.append(f'<text x="{x:.0f}" y="{y - 3:.0f}" text-anchor="middle" fill="#111827" font-weight="600">{html.escape(dev)}</text>')
        out.append(f'<text x="{x:.0f}" y="{y + 13:.0f}" text-anchor="middle" fill="#6b7480" font-size="10">{html.escape(asn)}</text>')
    out.append("</svg>")
    legend = ('<p style="font-size:12px;color:#6b7480;margin:4px 0 0">'
              '実線 = 主回線 / 破線 = 副回線 / 太線 = 1 Gbps 以上。青 = eBGP、紫 = iBGP、灰 = 拠点 LAN。'
              'アドレスと帯域はすべて架空（lab と同じ）。</p>')
    return "".join(out) + legend


# ---------------------------------------------------------------- chat
def invoke(prompt: str, session_id: str) -> str:
    try:
        res = agentcore.invoke_agent_runtime(
            agentRuntimeArn=ARN, runtimeSessionId=session_id, qualifier="DEFAULT",
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


with gr.Blocks(title=f"{TITLE} チャット") as demo:
    gr.Markdown(f"## {TITLE} チャット")
    with gr.Tab("チャット"):
        session = gr.State("")
        chatbot = gr.Chatbot(type="messages", height=480, label="会話")
        with gr.Row():
            box = gr.Textbox(placeholder="質問を入力（Enter で送信、Shift+Enter で改行）", max_length=MAX_PROMPT,
                             show_label=False, scale=6, lines=2)
            send = gr.Button("送信", variant="primary", scale=1)
            reset = gr.Button("新しい会話", scale=1)
        box.submit(respond, [box, chatbot, session], [box, chatbot, session])
        send.click(respond, [box, chatbot, session], [box, chatbot, session])
        reset.click(new_session, [chatbot, session], [chatbot, session])
        gr.Markdown("機器の一覧・接続・停止時の影響は、エージェントがトポロジのツールで調べて答えます（例: `hq-ce-01 の接続先は` / `carrier-pe-02 が落ちたら`）。")
    with gr.Tab("トポロジ"):
        gr.HTML(topology_svg())
        gr.Dataframe(device_table(), interactive=False, label="機器（devices.yaml）")

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(
        server_name="127.0.0.1", server_port=PORT, share=False, show_api=False, quiet=True,
    )
