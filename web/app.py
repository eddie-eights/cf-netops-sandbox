"""チャット Web（Gradio）。127.0.0.1 だけで待ち受け、利用者は SSM のポートフォワーディングで開く。

このファイルは画面の組み立てだけ。中身は 4 つのモジュールに分けてある:
  config.py         環境変数（.env / systemd）の読み出しと、agent/ のモジュールへのパス通し
  chat.py           「チャット」タブ。質問を AgentCore Runtime に送る
  topology_view.py  「トポロジ」タブ。SVG の図・機器の表・Neptune でのリンク編集
  incident_view.py  「異常一覧」「承認」タブ。DynamoDB の anomalies と proposals

agent/ の topology.py / anomalies.py / graph.py / proposals.py / toolkit.py をそのまま同じディレクトリに置いて import する
（terraform/base/core の出力 upload_web_command が web/*.py と一緒に S3 へ上げる）。
依存（gradio / boto3 / pyyaml）は S3 に置いた wheel から入れる（terraform/base/core の user_data）。インターネットには出ない。
"""

import gradio as gr
import pandas as pd

import chat
import incident_view as iv
import topology_view as tv
from config import MAX_PROMPT, PORT, TITLE

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
        box.submit(chat.respond, [box, chatbot, session], [box, chatbot, session])
        send.click(chat.respond, [box, chatbot, session], [box, chatbot, session])
        reset.click(chat.new_session, [chatbot, session], [chatbot, session])
        gr.Markdown("機器の一覧・接続・停止時の影響は、エージェントがトポロジのツールで調べて答えます（例: `hq-ce-01 の接続先は` / `carrier-pe-02 が落ちたら`）。")
    with gr.Tab("トポロジ"):
        topo_html = gr.HTML(tv.topology_svg())
        topo_table = gr.Dataframe(tv.device_table(), interactive=False, label="機器")
        topo_refresh = gr.Button("再読み込み")
        with gr.Accordion("Neptune で編集（terraform/pipeline/graph がある間だけ）", open=False):
            edit_msg = gr.Markdown("" if tv.can_edit() else "Neptune は未配備。terraform/pipeline/graph を apply して Web を再起動すると使えます。")
            gr.Markdown("**静的データを投入** = Neptune の中身をいったん全部消して、`agent/data/` の 10 台・10 本に戻す（初回と、編集をやり直したいとき）。"
                        "機器の追加・削除はこの画面にはないので `agent/data/` を直して投入し直す。リンクは下で 1 本ずつ足す・消す。"
                        "変えた内容はエージェントの次の質問から効く。")
            with gr.Row():
                seed_btn = gr.Button("静的データを投入（Neptune を消して 10 台・10 本に戻す）", interactive=tv.can_edit())
            gr.Markdown("#### リンクを追加")
            with gr.Row():
                la = gr.Dropdown(tv.device_choices(), value=None, label="機器 A", scale=2)
                lai = gr.Dropdown([], value=None, label="A のインタフェース", allow_custom_value=True, scale=2,
                                  info="機器 A を選ぶと使用中の名前が出る。新しい名前（eth3 など）も打てる")
                lb = gr.Dropdown(tv.device_choices(), value=None, label="機器 B", scale=2)
                lbi = gr.Dropdown([], value=None, label="B のインタフェース", allow_custom_value=True, scale=2,
                                  info="機器 B を選ぶと使用中の名前が出る。新しい名前も打てる")
            with gr.Row():
                lkind = gr.Dropdown([("ebgp（拠点 - キャリア）", "ebgp"), ("ibgp（キャリア内）", "ibgp"), ("l2（拠点 LAN）", "l2"), ("mgmt（管理）", "mgmt")],
                                    value="l2", label="種別")
                lrole = gr.Dropdown([("なし", ""), ("primary（主回線。図は実線）", "primary"), ("secondary（副回線。図は破線）", "secondary")],
                                    value="", label="役割")
                lbw = gr.Number(label="帯域 Mbps", precision=0, info="空でもよい。1000 以上は図で太線")
                add_btn = gr.Button("リンクを追加", variant="primary", interactive=tv.can_edit())
            gr.Markdown("#### リンクを削除")
            with gr.Row():
                del_sel = gr.Dropdown(tv.link_choices(), value=None, label="削除するリンク", scale=4,
                                      info="「機器 A の IF - 機器 B の IF [種別 役割]」。追加・削除・再読み込みのたびに更新")
                del_btn = gr.Button("選んだリンクを削除", variant="stop", interactive=tv.can_edit())
            edit_out = [edit_msg, topo_html, topo_table, la, lb, del_sel]
            la.change(tv.interface_choices, [la], [lai])
            lb.change(tv.interface_choices, [lb], [lbi])
            seed_btn.click(tv.seed_graph, [la, lb], edit_out)
            add_btn.click(tv.add_link, [la, lai, lb, lbi, lkind, lrole, lbw], edit_out)
            del_btn.click(tv.remove_link, [del_sel, la, lb], edit_out)
        topo_refresh.click(tv.refresh_topology, [la, lb], [topo_html, topo_table, la, lb, del_sel])
    with gr.Tab("異常一覧"):
        with gr.Row():
            an_status = gr.Radio(["open", "resolved"], value="open", label="状態（open = 未解消）", scale=3)
            an_refresh = gr.Button("更新", scale=1)
        an_msg = gr.Markdown()
        an_table = gr.Dataframe(pd.DataFrame(columns=iv.ANOMALY_COLS), interactive=False, wrap=True, label="異常（Spark が DynamoDB に書いたもの）")
        an_refresh.click(iv.anomaly_table, [an_status], [an_msg, an_table])
        an_status.change(iv.anomaly_table, [an_status], [an_msg, an_table])
        demo.load(iv.anomaly_table, [an_status], [an_msg, an_table])
        gr.Markdown("lab で `lab failover` を打つと、SNMP ポーリング（10 秒）か trap（5 秒）で `link_down` が出ます。`lab heal-main` で resolved に変わります。")
    with gr.Tab("承認"):
        with gr.Row():
            pr_status = gr.Radio(["pending", "approved", "applied", "verified", "failed", "rejected", "expired", "all"],
                                 value="pending", label="状態（pending = 承認待ち）", scale=4)
            pr_refresh = gr.Button("更新", scale=1)
        pr_msg = gr.Markdown()
        pr_table = gr.Dataframe(pd.DataFrame(columns=iv.PROPOSAL_COLS), interactive=False, wrap=True, column_widths=iv.PROPOSAL_WIDTHS,
                                label="修復案（ワーカーが DynamoDB に書いたもの。長い列は折り返し。全文は下の「詳細」）")
        with gr.Row():
            pr_id = gr.Dropdown([], value=None, allow_custom_value=True, scale=4,
                                label="proposal_id（表の 1 列目。選ぶと下に全文が出る）")
            pr_approve = gr.Button("承認して直す", variant="primary", scale=1)
            pr_reject = gr.Button("却下", scale=1)
        pr_detail = gr.Markdown(label="詳細")
        pr_out = [pr_msg, pr_table, pr_id]
        pr_refresh.click(iv.proposal_table, [pr_status], pr_out)
        pr_status.change(iv.proposal_table, [pr_status], pr_out)
        demo.load(iv.proposal_table, [pr_status], pr_out)
        pr_id.change(iv.proposal_detail, [pr_id], [pr_detail])
        # 30 秒ごとに描き直す（Spark の検知が 1 分、ワーカーの確認が 30 秒おきなので、ボタンを押さなくても追える。
        # 読むのは Neptune 1 回と DynamoDB のクエリ 2 回で、開いているブラウザの数だけ）。proposal_id の選択はそのまま残す
        ticker = gr.Timer(30)
        ticker.tick(tv.redraw_topology, None, [topo_html, topo_table])
        ticker.tick(iv.anomaly_table, [an_status], [an_msg, an_table])
        ticker.tick(lambda st: iv.proposal_table(st)[:2], [pr_status], [pr_msg, pr_table])
        pr_approve.click(lambda i, s: iv.decide_proposal(i, "approved", s), [pr_id, pr_status], pr_out)
        pr_reject.click(lambda i, s: iv.decide_proposal(i, "rejected", s), [pr_id, pr_status], pr_out)
        gr.Markdown("修復案は Temporal のワークフロー（terraform/workflow の ECS Fargate のワーカー）が出し、承認を待っています。"
                    "承認すると同じワークフローが lab EC2 で `sudo lab <コマンド>` を打ち（EC2 への入口は SSM Run Command。SSH は開けていない）、"
                    "異常が resolved になるまで 30 秒おきに数回確かめて verified にします。却下は何もしません。"
                    "承認待ちのまま 2 時間（approval_timeout_minutes）で expired になります。")

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(
        server_name="127.0.0.1", server_port=PORT, share=False, show_api=False, quiet=True,
    )
