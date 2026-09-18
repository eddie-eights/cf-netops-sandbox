"""「トポロジ」タブの中身。機器とリンクを SVG に描き、表に出し、Neptune があれば編集する。

元データはエージェントと同じ topology.py（Neptune があればそこから、無ければ data/ の静的データ）。
Neptune のときだけリンクの追加・削除と静的データからの投入ができる。lab（terraform/pipeline/lab）には触らない。
app.py が画面を組むのに要る選択肢は can_edit() / device_choices() / link_choices() で渡す。
"""

import html

import gradio as gr
import pandas as pd
from botocore.exceptions import BotoCoreError, ClientError

from config import log

import graph  # config が sys.path を通したあとで読む（agent/graph.py）
import topology

ROLE_ORDER = ["core", "pe", "distribution", "aggregation", "access", "ce", "host"]
ROLE_LABEL = {"pe": "キャリア PE", "ce": "拠点 CE", "host": "LAN 端末"}
DOWN_COLOR = "#c62828"


def can_edit() -> bool:
    """Neptune が配備されていれば編集できる（未配備なら編集のボタンを押せなくする）"""
    return graph.configured()


def device_choices() -> list:
    return sorted(topology.NODES)


def link_choices() -> list:
    return topology.link_choices()


# ---------------------------------------------------------------- 表示（表と図）
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
            "監視": "対象" if d.get("enabled") else "対象外", "状態": d.get("status") or "UP",
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
        down = (l.get("status") or "UP") != "UP"   # Spark の検知で付いた動的な状態（graph.set_status）
        title = (f'{l["a"]} {l["a_if"]} - {l["b"]} {l["b_if"]} ({l["kind"]}{" " + l["role"] if l.get("role") else ""}, {l.get("bandwidth_mbps")} Mbps'
                 f'{", " + l["status"] if down else ""})')
        stroke = DOWN_COLOR if down else color.get(l["kind"], "#999")
        out.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="{stroke}" '
                   f'stroke-width="{w + 1 if down else w}"{dash}><title>{html.escape(title)}</title></line>')
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        out.append(f'<text x="{mx:.0f}" y="{my - 4:.0f}" text-anchor="middle" fill="#4b5563" font-size="10">'
                   f'{html.escape((l.get("a_if") or "") + "/" + (l.get("b_if") or ""))}</text>')
    for dev, (x, y) in pos.items():
        n = topology.NODES[dev]
        fill = {"pe": "#e8f0fe", "ce": "#e6f4ea", "host": "#f3f4f6"}.get(n["role"], "#fff")
        asn = f'AS {n["asn"]}' if n.get("asn") else n["site"]
        st = n.get("status") or "UP"
        border = f'stroke="{DOWN_COLOR}" stroke-width="2.5"' if st != "UP" else 'stroke="#374151" stroke-width="1.2"'
        out.append(f'<rect x="{x - node_w / 2:.0f}" y="{y - node_h / 2:.0f}" width="{node_w}" height="{node_h}" rx="6" '
                   f'fill="{fill}" {border}><title>{html.escape(dev + " " + st)}</title></rect>')
        out.append(f'<text x="{x:.0f}" y="{y - 3:.0f}" text-anchor="middle" fill="#111827" font-weight="600">{html.escape(dev)}</text>')
        out.append(f'<text x="{x:.0f}" y="{y + 13:.0f}" text-anchor="middle" fill="#6b7480" font-size="10">{html.escape(asn)}</text>')
    out.append("</svg>")
    src = {"neptune": "Neptune（terraform/pipeline/graph）", "neptune-empty": "Neptune は空。静的データを表示中（下の「静的データを投入」で入る）"}.get(
        topology.SOURCE, "静的データ（data/。terraform/pipeline/graph を apply すると Neptune に切り替わる）")
    legend = ('<p style="font-size:12px;color:#6b7480;margin:4px 0 0">'
              '実線 = 主回線 / 破線 = 副回線 / 太線 = 1 Gbps 以上。青 = eBGP、紫 = iBGP、灰 = 拠点 LAN。'
              '<span style="color:#c62828">赤</span> = 落ちている（Spark の検知が Neptune の status に反映したもの。復旧すると戻る）。'
              f'アドレスと帯域はすべて架空（lab と同じ）。元データ: {html.escape(src)}</p>')
    return "".join(out) + legend


def _choices(a="", b=""):
    """編集画面の選択肢（機器 A / B と削除するリンク）を、読み直したトポロジから作り直す。選んでいた機器は残す"""
    devs = device_choices()
    return (gr.update(choices=devs, value=a if a in devs else None),
            gr.update(choices=devs, value=b if b in devs else None),
            gr.update(choices=link_choices(), value=None))


def refresh_topology(a="", b=""):
    topology.reload(force=True)
    return topology_svg(), device_table(), *_choices(a, b)


def redraw_topology():
    """自動更新用。図と表だけ描き直し、編集フォームの選択には触らない"""
    topology.reload(force=True)
    return topology_svg(), device_table()


def interface_choices(device):
    """機器を選んだら、その機器でいま使われているインタフェース名を選択肢に出す（新しい名前は打てる）"""
    return gr.update(choices=topology.interfaces(device) if device else [], value=None)


# ---------------------------------------------------------------- 編集（Neptune があるときだけ）
def _graph_call(fn, *args, a="", b=""):
    """Neptune の編集。結果のメッセージと、描き直した図・表・選択肢を返す"""
    if not graph.configured():
        return "Neptune は未配備（terraform/pipeline/graph）", *refresh_topology(a, b)
    try:
        r = fn(*args)
    except (ClientError, BotoCoreError, KeyError, ValueError, TypeError) as e:
        log.error("neptune write failed: %s", str(e)[:500])
        return f"Neptune の更新に失敗: {str(e)[:200]}", *refresh_topology(a, b)
    msg = r.get("error") or ", ".join(f"{k}: {v}" for k, v in r.items())
    return msg, *refresh_topology(a, b)


def seed_graph(a, b):
    return _graph_call(lambda: graph.seed(*topology.load_static()), a=a, b=b)


def add_link(a, a_if, b, b_if, kind, role, bw):
    a, a_if, b, b_if = (str(x or "").strip() for x in (a, a_if, b, b_if))
    if not (a and b):
        return "機器 A と機器 B を選んでください", *refresh_topology(a, b)
    if a == b:
        return "機器 A と機器 B が同じです", *refresh_topology(a, b)
    if not (a_if and b_if):
        return "両端のインタフェース名を選ぶか入力してください（例 eth3）", *refresh_topology(a, b)
    return _graph_call(graph.add_link, a, a_if, b, b_if, kind, role or "", int(bw) if bw else None, a=a, b=b)


def remove_link(sel, a, b):
    """sel は削除用 Dropdown の値 "a|a_if|b"（topology.link_choices）"""
    if not sel or str(sel).count("|") != 2:
        return "削除するリンクを一覧から選んでください", *refresh_topology(a, b)
    la, a_if, lb = str(sel).split("|", 2)
    return _graph_call(graph.remove_link, la, lb, a_if, a=a, b=b)
