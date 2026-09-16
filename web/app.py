"""チャット Web（Gradio）。127.0.0.1 だけで待ち受け、利用者は SSM のポートフォワーディングで開く。

タブは 4 つ:
  - チャット: 質問を AgentCore Runtime に送る（boto3 の invoke_agent_runtime。署名はインスタンスロール）。
    セッション ID はブラウザのセッションごとに 1 つ（Runtime 側の会話履歴はこの ID で分かれる）
  - トポロジ: 段（pe / ce / host）に分けて SVG に描き、機器を表で出す。元データはエージェントと同じ
    topology.py（Neptune があればそこから、無ければ data/ の静的データ）。Neptune のときはリンクの追加・削除と
    静的データからの投入がここでできる。lab（terraform/lab）には触らない
  - 異常一覧: terraform/stream の detector が DynamoDB に書いた異常（anomalies.py）。未配備なら案内だけ出す
  - 承認: terraform/workflow のワーカーが出した修復案（proposals.py）を見て、承認か却下を書き戻す。未配備なら案内だけ出す

agent/ の topology.py / anomalies.py / graph.py / proposals.py をそのまま同じディレクトリに置いて import する（terraform/main の出力 upload_web_command）。
依存（gradio / boto3 / pyyaml）は S3 に置いた wheel から入れる（terraform/main の user_data）。インターネットには出ない。
"""

import html
import json
import logging
import os
import sys
import uuid

import boto3
import gradio as gr
import pandas as pd
from botocore.exceptions import BotoCoreError, ClientError

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env_file() -> str:
    """手元で動かすときの .env（リポジトリ直下。ENV_FILE=<パス> で変えられる。並びは .env.example）。
    EC2 では systemd の EnvironmentFile が渡すので無くてよい。既にある環境変数は上書きしない。同じ名前は後の行が勝つ。
    読めたらそのパスを返す"""
    path = os.environ.get("ENV_FILE") or os.path.join(HERE, "..", ".env")
    if not os.path.isfile(path):
        return ""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k and v:
                values[k] = v
    for k, v in values.items():
        os.environ.setdefault(k, v)
    return os.path.abspath(path)


ENV_FILE = load_env_file()
_missing = [k for k in ("RUNTIME_ARN", "AWS_REGION") if not os.environ.get(k)]
if _missing:
    sys.exit(f"environment variable {' / '.join(_missing)} is not set. "
             "EC2: /etc/<name_prefix>-web.env is written by the user_data of terraform/main (compare with .env.example). "
             "local: cp .env.example .env and fill it in (README)")
ARN = os.environ["RUNTIME_ARN"]
REGION = os.environ["AWS_REGION"]
PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(HERE, "data")
if not os.path.isabs(DATA_DIR):  # 相対パスはリポジトリ直下から（.env.example の DATA_DIR=agent/data）
    DATA_DIR = os.path.normpath(os.path.join(HERE, "..", DATA_DIR))
TITLE = os.environ.get("TITLE", "NWC PoC")
MAX_PROMPT = 4000

# エージェントと同じモジュール。EC2 では同じディレクトリに置く（upload_web_command）。手元ではリポジトリの agent/ から読む。
# 静的データの場所だけ DATA_DIR に合わせる
if not os.path.isfile(os.path.join(HERE, "topology.py")):
    sys.path.append(os.path.join(HERE, "..", "agent"))
os.environ["TOPOLOGY_DATA_DIR"] = DATA_DIR
import anomalies  # noqa: E402
import proposals  # noqa: E402
import graph  # noqa: E402
import topology  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("web")
# 読み取りのタイムアウトは Runtime の応答（ツール往復を含む）より長くする
agentcore = boto3.client("bedrock-agentcore", region_name=REGION,
                         config=boto3.session.Config(read_timeout=150, connect_timeout=10, retries={"max_attempts": 1}))


# ---------------------------------------------------------------- data
ROLE_ORDER = ["core", "pe", "distribution", "aggregation", "access", "ce", "host"]
ROLE_LABEL = {"pe": "キャリア PE", "ce": "拠点 CE", "host": "LAN 端末"}


def device_table() -> pd.DataFrame:
    topology.reload()
    degree = {}
    for l in topology.LINKS:
        degree[l["a"]] = degree.get(l["a"], 0) + 1
        degree[l["b"]] = degree.get(l["b"], 0) + 1
    rows = []
    for d in topology.DEVICES:
        rows.append({
            "機器": d["device_id"], "拠点": d["site"], "役割": d["role"], "AS": d.get("asn") or "",
            "管理 IP": d.get("mgmt_ip") or "", "リンク数": degree.get(d["device_id"], 0),
            "監視": "対象" if d.get("enabled") else "対象外",
        })
    rows.sort(key=lambda r: (ROLE_ORDER.index(r["役割"]) if r["役割"] in ROLE_ORDER else 99, r["機器"]))
    return pd.DataFrame(rows)


def topology_svg() -> str:
    """段ごとに横へ並べた素朴な図。位置は device_id の順で決まるので、再読み込みしても動かない"""
    topology.reload()
    layers = {}
    for n in topology.DEVICES:
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
    for l in topology.LINKS:
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
                   f'{html.escape((l.get("a_if") or "") + "/" + (l.get("b_if") or ""))}</text>')
    for dev, (x, y) in pos.items():
        n = topology.NODES[dev]
        fill = {"pe": "#e8f0fe", "ce": "#e6f4ea", "host": "#f3f4f6"}.get(n["role"], "#fff")
        asn = f'AS {n["asn"]}' if n.get("asn") else n["site"]
        out.append(f'<rect x="{x - node_w / 2:.0f}" y="{y - node_h / 2:.0f}" width="{node_w}" height="{node_h}" rx="6" '
                   f'fill="{fill}" stroke="#374151" stroke-width="1.2"/>')
        out.append(f'<text x="{x:.0f}" y="{y - 3:.0f}" text-anchor="middle" fill="#111827" font-weight="600">{html.escape(dev)}</text>')
        out.append(f'<text x="{x:.0f}" y="{y + 13:.0f}" text-anchor="middle" fill="#6b7480" font-size="10">{html.escape(asn)}</text>')
    out.append("</svg>")
    src = {"neptune": "Neptune（terraform/graph）", "neptune-empty": "Neptune は空。静的データを表示中（下の「静的データを投入」で入る）"}.get(
        topology.SOURCE, "静的データ（data/。terraform/graph を apply すると Neptune に切り替わる）")
    legend = ('<p style="font-size:12px;color:#6b7480;margin:4px 0 0">'
              '実線 = 主回線 / 破線 = 副回線 / 太線 = 1 Gbps 以上。青 = eBGP、紫 = iBGP、灰 = 拠点 LAN。'
              f'アドレスと帯域はすべて架空（lab と同じ）。元データ: {html.escape(src)}</p>')
    return "".join(out) + legend


def refresh_topology():
    topology.reload(force=True)
    return topology_svg(), device_table()


def _graph_call(fn, *args):
    """Neptune の編集。結果のメッセージと、描き直した図・表を返す"""
    if not graph.configured():
        return "Neptune は未配備（terraform/graph）", *refresh_topology()
    try:
        r = fn(*args)
    except (ClientError, BotoCoreError, KeyError, ValueError, TypeError) as e:
        log.error("neptune write failed: %s", str(e)[:500])
        return f"Neptune の更新に失敗: {str(e)[:200]}", *refresh_topology()
    msg = r.get("error") or ", ".join(f"{k}: {v}" for k, v in r.items())
    return msg, *refresh_topology()


def seed_graph():
    return _graph_call(lambda: graph.seed(*topology.load_static()))


def add_link(a, a_if, b, b_if, kind, role, bw):
    if not (a and a_if and b and b_if):
        return "両端の機器とインタフェースを入れてください", *refresh_topology()
    return _graph_call(graph.add_link, a.strip(), a_if.strip(), b.strip(), b_if.strip(), kind, role or "", int(bw) if bw else None)


def remove_link(a, b, a_if):
    if not (a and b):
        return "両端の機器を入れてください", *refresh_topology()
    return _graph_call(graph.remove_link, a.strip(), b.strip(), (a_if or "").strip())


# ---------------------------------------------------------------- anomalies
ANOMALY_COLS = ["機器", "種別", "対象", "状態", "発生", "最終確認", "解消", "経路"]


# ---------------------------------------------------------------- proposals (phase 3)
PROPOSAL_COLS = ["proposal_id", "状態", "機器", "種別", "対象", "原因", "処置", "コマンド", "理由", "作成", "更新", "決めた人", "結果"]


def proposal_table(status: str = "pending"):
    r = proposals.list_proposals(status=status, limit=100)
    rows = [{"proposal_id": p.get("proposal_id", ""), "状態": p.get("status", ""), "機器": p.get("device_id", ""),
             "種別": p.get("kind", ""), "対象": p.get("target", ""), "原因": p.get("cause", ""), "処置": p.get("action", ""),
             "コマンド": p.get("command", ""), "理由": p.get("reason", ""), "作成": p.get("created_at_jst", ""),
             "更新": p.get("updated_at_jst", ""), "決めた人": p.get("decided_by", ""),
             "結果": (p.get("verify_note") or p.get("apply_output") or "")[:200]} for p in r.get("proposals", [])]
    msg = r["error"] if r.get("error") else f"{r.get('count', 0)} 件（{status}）"
    return msg, pd.DataFrame(rows, columns=PROPOSAL_COLS)


def decide_proposal(proposal_id: str, decision: str, status: str):
    r = proposals.decide((proposal_id or "").strip(), decision, decided_by="web")
    msg = r["error"] if r.get("error") else f"{r['proposal_id']} を {decision} にした（ワーカーが次の段に進める）"
    _, table = proposal_table(status)
    return msg, table


def anomaly_table(status: str = "open"):
    r = anomalies.list_anomalies(status=status, limit=100)
    rows = [{"機器": a.get("device_id", ""), "種別": a.get("kind", ""), "対象": a.get("target", ""), "状態": a.get("status", ""),
             "発生": a.get("first_seen_jst", ""), "最終確認": a.get("last_seen_jst", ""), "解消": a.get("resolved_at_jst", ""),
             "経路": a.get("source", "")} for a in r.get("anomalies", [])]
    msg = r["error"] if r.get("error") else f"{r.get('count', 0)} 件（{'未解消' if status == 'open' else '解消済み'}）"
    return msg, pd.DataFrame(rows, columns=ANOMALY_COLS)


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
        topo_html = gr.HTML(topology_svg())
        topo_table = gr.Dataframe(device_table(), interactive=False, label="機器")
        topo_refresh = gr.Button("再読み込み")
        topo_refresh.click(refresh_topology, [], [topo_html, topo_table])
        with gr.Accordion("Neptune で編集（terraform/graph を apply したとき）", open=False):
            edit_msg = gr.Markdown("" if graph.configured() else "Neptune は未配備。terraform/graph を apply して Web を再起動すると使えます。")
            with gr.Row():
                seed_btn = gr.Button("静的データを投入（全部置き換え）", interactive=graph.configured())
            with gr.Row():
                la = gr.Textbox(label="機器 A", scale=2); lai = gr.Textbox(label="A のインタフェース", scale=2)
                lb = gr.Textbox(label="機器 B", scale=2); lbi = gr.Textbox(label="B のインタフェース", scale=2)
            with gr.Row():
                lkind = gr.Dropdown(["ebgp", "ibgp", "l2", "mgmt"], value="l2", label="種別")
                lrole = gr.Dropdown(["", "primary", "secondary"], value="", label="役割")
                lbw = gr.Number(label="帯域 Mbps", precision=0)
                add_btn = gr.Button("リンクを追加", interactive=graph.configured())
                del_btn = gr.Button("A - B のリンクを削除", interactive=graph.configured())
            seed_btn.click(seed_graph, [], [edit_msg, topo_html, topo_table])
            add_btn.click(add_link, [la, lai, lb, lbi, lkind, lrole, lbw], [edit_msg, topo_html, topo_table])
            del_btn.click(remove_link, [la, lb, lai], [edit_msg, topo_html, topo_table])
    with gr.Tab("異常一覧"):
        with gr.Row():
            an_status = gr.Radio(["open", "resolved"], value="open", label="状態（open = 未解消）", scale=3)
            an_refresh = gr.Button("更新", scale=1)
        an_msg = gr.Markdown()
        an_table = gr.Dataframe(pd.DataFrame(columns=ANOMALY_COLS), interactive=False, label="異常（detector が DynamoDB に書いたもの）")
        an_refresh.click(anomaly_table, [an_status], [an_msg, an_table])
        an_status.change(anomaly_table, [an_status], [an_msg, an_table])
        demo.load(anomaly_table, [an_status], [an_msg, an_table])
        gr.Markdown("lab で `lab failover` を打つと、SNMP ポーリング（10 秒）か trap（5 秒）で `link_down` が出ます。`lab heal-main` で resolved に変わります。")
    with gr.Tab("承認"):
        with gr.Row():
            pr_status = gr.Radio(["pending", "approved", "applied", "verified", "failed", "rejected", "expired", "all"],
                                 value="pending", label="状態（pending = 承認待ち）", scale=4)
            pr_refresh = gr.Button("更新", scale=1)
        pr_msg = gr.Markdown()
        pr_table = gr.Dataframe(pd.DataFrame(columns=PROPOSAL_COLS), interactive=False, label="修復案（ワーカーが DynamoDB に書いたもの）")
        with gr.Row():
            pr_id = gr.Textbox(label="proposal_id（表の 1 列目をそのまま）", scale=4)
            pr_approve = gr.Button("承認して直す", variant="primary", scale=1)
            pr_reject = gr.Button("却下", scale=1)
        pr_refresh.click(proposal_table, [pr_status], [pr_msg, pr_table])
        pr_status.change(proposal_table, [pr_status], [pr_msg, pr_table])
        demo.load(proposal_table, [pr_status], [pr_msg, pr_table])
        pr_approve.click(lambda i, s: decide_proposal(i, "approved", s), [pr_id, pr_status], [pr_msg, pr_table])
        pr_reject.click(lambda i, s: decide_proposal(i, "rejected", s), [pr_id, pr_status], [pr_msg, pr_table])
        gr.Markdown("承認すると、ワーカーが lab EC2 で `sudo lab <コマンド>` を打ち（SSM Run Command）、異常が resolved になるまで数回確かめます。"
                    "却下は何もしません。承認待ちのまま 2 時間（approval_timeout_minutes）で expired になります。")

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(
        server_name="127.0.0.1", server_port=PORT, share=False, show_api=False, quiet=True,
    )
