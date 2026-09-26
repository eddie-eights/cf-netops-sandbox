"""トポロジをエージェントのツールとして出す。

元データは 2 通り。Neptune（terraform/pipeline/graph。graph.configured() が真）があればそこから読み、無ければ
静的データ（data/devices.yaml と data/topology.json と data/layers.json。tools Lambda では devices.json）。どちらも中身はローカル lab
（lab/splab.clab.yml。Spine-Leaf の 8 台）そのもので、すべて架空のアドレス。SNMP や lab には触らない。読み取りだけなので、モデルが何度呼んでも副作用は無い。
Neptune のときは TTL 秒ごとに読み直す（画面で編集した結果が次の質問に効く）。

物理層（機器と回線）のほかに、IP 層（ip_interface / isis_adjacency）と EVPN・BGP 層（bgp_session / evpn_instance / ethernet_segment）を
layers ツールで出す（頂点の id と、下の層を指す interface_id / ip_interface_id で層をまたいで追える。agent/graph.py の docstring）。

Converse の toolConfig に渡す仕様（TOOL_SPECS）と、toolUse を受けて実行する run_tool() を持つ。
"""

import json
import logging
import os
import time
from collections import deque

from botocore.exceptions import BotoCoreError, ClientError

import graph
import toolkit

DATA_DIR = os.environ.get("TOPOLOGY_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
# 段の順（上が上流）。Web の図の段もこれ。unknown は検知が先に来た未登録の機器（graph.set_status が作る）
ROLE_ORDER = ["upstream", "leafsw", "spine", "leaf", "host", "unknown"]
LAYER_NAMES = ("ip", "evpn")
MAX_HOPS = 6
TTL = int(os.environ.get("TOPOLOGY_TTL", "60"))
log = logging.getLogger("topology")


def load_static() -> tuple[list[dict], list[dict]]:
    """data/ の静的データ。devices の各行に topology.json の asn を足して返す（Neptune の seed にも使う）"""
    # tools Lambda（terraform/workflow）には PyYAML が無いので、Terraform が JSON にした devices.json を先に見る
    devices_json = os.path.join(DATA_DIR, "devices.json")
    if os.path.exists(devices_json):
        with open(devices_json, encoding="utf-8") as f:
            devices = json.load(f)["devices"]
    else:
        import yaml  # noqa: PLC0415 - Runtime イメージだけが持つ

        with open(os.path.join(DATA_DIR, "devices.yaml"), encoding="utf-8") as f:
            devices = yaml.safe_load(f)["devices"]
    with open(os.path.join(DATA_DIR, "topology.json"), encoding="utf-8") as f:
        topo = json.load(f)
    asn = {n["device_id"]: n.get("asn") for n in topo["nodes"]}
    return [{**d, "asn": asn.get(d["device_id"])} for d in devices], topo["links"]


def load_static_layers() -> dict:
    """data/layers.json（lab/lab_topology.py --layers の出力）。無ければ空"""
    path = os.path.join(DATA_DIR, "layers.json")
    if not os.path.exists(path):
        return {"vertices": [], "edges": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _build(devices: list[dict], links: list[dict]):
    # links は a < b で正規化してあるが、探索は無向で見る
    adj: dict[str, list[dict]] = {d["device_id"]: [] for d in devices}
    for l in links:
        for me, peer, my_if, peer_if in ((l["a"], l["b"], l["a_if"], l["b_if"]), (l["b"], l["a"], l["b_if"], l["a_if"])):
            adj.setdefault(me, []).append({
                "device_id": peer, "local_if": my_if, "remote_if": peer_if,
                "kind": l["kind"], "role": l.get("role") or "", "bandwidth_mbps": l.get("bandwidth_mbps"),
                "status": l.get("status") or "UP",   # Neptune の辺の動的な状態（graph.set_status）。静的データには無いので UP
            })
    return devices, {d["device_id"]: d for d in devices}, links, adj


DEVICES: list[dict] = []
NODES: dict[str, dict] = {}
LINKS: list[dict] = []
ADJ: dict[str, list[dict]] = {}
DEVICE_BY_ID: dict[str, dict] = {}
LAYERS: dict = {"vertices": [], "edges": []}
SOURCE = "static"
_loaded_at = 0.0


def reload(force: bool = False) -> str:
    """Neptune があればそこから、無ければ静的データから組み直す。戻り値は使った元（neptune / static）"""
    global DEVICES, NODES, LINKS, ADJ, DEVICE_BY_ID, LAYERS, SOURCE, _loaded_at
    if not force and _loaded_at and (SOURCE == "static" or time.time() - _loaded_at < TTL):
        return SOURCE
    source, devices, links, layers = "static", None, None, None
    if graph.configured():
        try:
            devices, links = graph.load_topology()
            source = "neptune" if devices else "neptune-empty"  # 空なら静的データを見せる（画面の「投入」で入れる）
            if devices:
                layers = graph.load_layers()
        except (ClientError, BotoCoreError, KeyError, ValueError, TypeError) as e:
            log.warning("neptune read failed, using static data: %s", str(e)[:200])
            devices = None
    if not devices:
        devices, links = load_static()
        layers = load_static_layers()
    SOURCE = source
    DEVICES, NODES, LINKS, ADJ = _build(devices, links)
    DEVICE_BY_ID = NODES
    LAYERS = layers or {"vertices": [], "edges": []}
    _loaded_at = time.time()
    return SOURCE


reload(force=True)


def _public(d: dict) -> dict:
    """SNMP のコミュニティなど、答えに出す必要のない項目を落とす"""
    return {
        "device_id": d["device_id"], "site": d["site"], "role": d["role"], "mgmt_ip": d.get("mgmt_ip"),
        "asn": d.get("asn"), "monitored": bool(d.get("enabled")), "status": d.get("status") or "UP",
        "registered": d.get("registered") is not False,
    }


def list_devices(site: str = "", role: str = "") -> dict:
    rows = [_public(d) for d in DEVICES if (not site or d["site"] == site) and (not role or d["role"] == role)]
    rows.sort(key=lambda r: (ROLE_ORDER.index(r["role"]) if r["role"] in ROLE_ORDER else 99, r["device_id"]))
    return {"count": len(rows), "devices": rows}


def neighbors(device_id: str) -> dict:
    if device_id not in DEVICE_BY_ID:
        return {"error": f"{device_id} はトポロジに無い", "known": sorted(DEVICE_BY_ID)}
    return {"device_id": device_id, "neighbors": ADJ.get(device_id, [])}


def blast_radius(device_id: str, max_hops: int = 2) -> dict:
    """device_id が落ちたとき、そこから max_hops 以内にある機器。上流を経由しないと届かない拠点の目安"""
    if device_id not in DEVICE_BY_ID:
        return {"error": f"{device_id} はトポロジに無い", "known": sorted(DEVICE_BY_ID)}
    max_hops = max(1, min(int(max_hops), MAX_HOPS))
    seen = {device_id: 0}
    q = deque([device_id])
    while q:
        cur = q.popleft()
        if seen[cur] >= max_hops:
            continue
        for nb in ADJ.get(cur, []):
            if nb["device_id"] not in seen:
                seen[nb["device_id"]] = seen[cur] + 1
                q.append(nb["device_id"])
    affected = [{"device_id": k, "hops": v, "site": DEVICE_BY_ID[k]["site"], "role": DEVICE_BY_ID[k]["role"],
                 "status": DEVICE_BY_ID[k].get("status") or "UP"}
                for k, v in seen.items() if k != device_id]
    affected.sort(key=lambda r: (r["hops"], r["device_id"]))
    return {"device_id": device_id, "max_hops": max_hops, "affected": affected}


def topology_graph() -> dict:
    return {"source": SOURCE, "nodes": [_public(d) for d in DEVICES], "links": LINKS}


def layers(device_id: str = "", layer: str = "") -> dict:
    """物理層より上の頂点と辺。device_id があればその機器のもの（辺は片端がその機器のもの）、layer（ip / evpn）があればその層だけ。
    頂点の status は動的な状態（gNMI の検知。無ければ UP）"""
    if device_id and device_id not in DEVICE_BY_ID:
        return {"error": f"{device_id} はトポロジに無い", "known": sorted(DEVICE_BY_ID)}
    if layer and layer not in LAYER_NAMES:
        return {"error": f"layer は {' / '.join(LAYER_NAMES)} のどれか"}
    rows = [{**v, "status": v.get("status") or "UP"} for v in LAYERS.get("vertices") or []
            if (not device_id or v.get("device_id") == device_id) and (not layer or v.get("layer") == layer)]
    ids = {v["id"] for v in rows}
    edges = [e for e in LAYERS.get("edges") or [] if e.get("from") in ids or e.get("to") in ids]
    return {"device_id": device_id, "layer": layer, "count": len(rows), "vertices": rows, "edges": edges}


def interfaces(device_id: str) -> list[str]:
    """device_id のインタフェース名（Web の編集画面の選択肢）。Neptune に lab の定義から入れた一覧があればそれと、
    つながるリンクに出てくるその機器側の名前（静的データには一覧が無いので、いま使われているものだけ）"""
    names = {l["a_if"] if l["a"] == device_id else l["b_if"] for l in LINKS if device_id in (l["a"], l["b"])}
    names |= {i.get("name") for i in (DEVICE_BY_ID.get(device_id) or {}).get("interfaces") or []}
    return sorted((n for n in names if n), key=lambda n: (len(n), n))


def link_choices() -> list[tuple[str, str]]:
    """Web の削除用。(表示, 値) の並びで、値は "a|a_if|b"（graph.remove_link の引数に戻す。機器名と IF 名に | は無い）"""
    out = []
    for l in LINKS:
        extra = l.get("kind") or ""
        if l.get("role"):
            extra += " " + l["role"]
        out.append((f'{l["a"]} {l["a_if"]} - {l["b"]} {l["b_if"]}  [{extra}]', f'{l["a"]}|{l["a_if"] or ""}|{l["b"]}'))
    return out


TOOL_SPECS = [
    {"toolSpec": {
        "name": "list_devices",
        "description": "監視対象ネットワークの機器一覧（拠点 site、役割 role、管理 IP、AS 番号、いまの状態 status = UP / DOWN / ALARM、registered = false はトポロジに未登録で検知だけが来た機器 role=unknown）。site や role で絞れる。",
        "inputSchema": {"json": {"type": "object", "properties": {
            "site": {"type": "string", "description": "拠点名で絞る（dc1 / wan）。空なら全部"},
            "role": {"type": "string", "description": "役割で絞る（leafsw = 上流側の Leaf / spine / leaf = アクセス側の Leaf / upstream = 上流の VM / host = アクセス側の VM / unknown）。空なら全部"},
        }}},
    }},
    {"toolSpec": {
        "name": "neighbors",
        "description": "機器の隣接（接続先の機器、両端のインタフェース名、回線の種別 fabric = Spine と Leaf のあいだ / lag = VM と Leaf の LACP / l2、主副、帯域、回線のいまの状態 status = UP / DOWN）。",
        "inputSchema": {"json": {"type": "object", "required": ["device_id"], "properties": {
            "device_id": {"type": "string", "description": "機器名（例 dc1-leaf-01）"},
        }}},
    }},
    {"toolSpec": {
        "name": "blast_radius",
        "description": "機器が停止したときに影響が及ぶ範囲（指定ホップ数以内の機器と拠点。各機器のいまの状態 status 付き）。",
        "inputSchema": {"json": {"type": "object", "required": ["device_id"], "properties": {
            "device_id": {"type": "string", "description": "停止を想定する機器名"},
            "max_hops": {"type": "integer", "description": "何ホップ先まで見るか（既定 2、最大 6）"},
        }}},
    }},
    {"toolSpec": {
        "name": "topology_graph",
        "description": "ネットワーク全体のノードとリンクの一覧（物理層）。全体像を説明するときに使う。",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }},
    {"toolSpec": {
        "name": "layers",
        "description": "物理層より上の情報。IP 層（ip_interface = サブインタフェースのアドレス、isis_adjacency = IS-IS の隣接）と EVPN・BGP 層（bgp_session = iBGP EVPN のセッション（Spine がルートリフレクタ）、evpn_instance = EVI / VNI / VTEP、ethernet_segment = LACP の multihoming の ESI）。各頂点は interface_id / ip_interface_id で下の層の頂点を指し、辺 over / peer / tunnel / attach / segment でつながる。各頂点のいまの状態 status = UP / DOWN 付き。「BGP のセッションは」「IS-IS の隣接は」「EVPN は」と聞かれたら使う。",
        "inputSchema": {"json": {"type": "object", "properties": {
            "device_id": {"type": "string", "description": "機器名（例 dc1-leaf-01）。空なら全機器"},
            "layer": {"type": "string", "description": "ip か evpn。空なら両方"},
        }}},
    }},
]
TOOLS = {"list_devices": list_devices, "neighbors": neighbors, "blast_radius": blast_radius, "topology_graph": topology_graph, "layers": layers}
# ツールを呼ぶ前に reload()（TTL を過ぎていれば Neptune を読み直す。画面での編集が次の質問に効く）
run_tool = toolkit.runner(TOOLS, before=reload)
