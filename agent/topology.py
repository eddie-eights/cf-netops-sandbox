"""トポロジをエージェントのツールとして出す。

元データは 2 通り。Neptune（terraform/graph。graph.configured() が真）があればそこから読み、無ければ
静的データ（data/devices.yaml と data/topology.json）。どちらも中身はローカル lab（lab/wvs2.clab.yml）の
10 台そのもので、すべて架空のアドレス。SNMP や lab には触らない。読み取りだけなので、モデルが何度呼んでも副作用は無い。
Neptune のときは TTL 秒ごとに読み直す（画面で編集した結果が次の質問に効く）。

Converse の toolConfig に渡す仕様（TOOL_SPECS）と、toolUse を受けて実行する run_tool() を持つ。
"""

import json
import logging
import os
import time
from collections import deque

import yaml
from botocore.exceptions import BotoCoreError, ClientError

import graph

DATA_DIR = os.environ.get("TOPOLOGY_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
# 段の順（上が上流）。frontend の ROLE_ORDER と同じ
ROLE_ORDER = ["core", "pe", "distribution", "aggregation", "access", "ce", "host"]
MAX_HOPS = 6
TTL = int(os.environ.get("TOPOLOGY_TTL", "60"))
log = logging.getLogger("topology")


def load_static() -> tuple[list[dict], list[dict]]:
    """data/ の静的データ。devices の各行に topology.json の asn を足して返す（Neptune の seed にも使う）"""
    with open(os.path.join(DATA_DIR, "devices.yaml"), encoding="utf-8") as f:
        devices = yaml.safe_load(f)["devices"]
    with open(os.path.join(DATA_DIR, "topology.json"), encoding="utf-8") as f:
        topo = json.load(f)
    asn = {n["device_id"]: n.get("asn") for n in topo["nodes"]}
    return [{**d, "asn": asn.get(d["device_id"])} for d in devices], topo["links"]


def _build(devices: list[dict], links: list[dict]):
    # links は a < b で正規化してあるが、探索は無向で見る
    adj: dict[str, list[dict]] = {d["device_id"]: [] for d in devices}
    for l in links:
        for me, peer, my_if, peer_if in ((l["a"], l["b"], l["a_if"], l["b_if"]), (l["b"], l["a"], l["b_if"], l["a_if"])):
            adj.setdefault(me, []).append({
                "device_id": peer, "local_if": my_if, "remote_if": peer_if,
                "kind": l["kind"], "role": l.get("role") or "", "bandwidth_mbps": l.get("bandwidth_mbps"),
            })
    return devices, {d["device_id"]: d for d in devices}, links, adj


DEVICES: list[dict] = []
NODES: dict[str, dict] = {}
LINKS: list[dict] = []
ADJ: dict[str, list[dict]] = {}
DEVICE_BY_ID: dict[str, dict] = {}
SOURCE = "static"
_loaded_at = 0.0


def reload(force: bool = False) -> str:
    """Neptune があればそこから、無ければ静的データから組み直す。戻り値は使った元（neptune / static）"""
    global DEVICES, NODES, LINKS, ADJ, DEVICE_BY_ID, SOURCE, _loaded_at
    if not force and _loaded_at and (SOURCE == "static" or time.time() - _loaded_at < TTL):
        return SOURCE
    source, devices, links = "static", None, None
    if graph.configured():
        try:
            devices, links = graph.load_topology()
            source = "neptune" if devices else "neptune-empty"  # 空なら静的データを見せる（画面の「投入」で入れる）
        except (ClientError, BotoCoreError, KeyError, ValueError, TypeError) as e:
            log.warning("neptune read failed, using static data: %s", str(e)[:200])
    if not devices:
        devices, links = load_static()
    SOURCE = source
    DEVICES, NODES, LINKS, ADJ = _build(devices, links)
    DEVICE_BY_ID = NODES
    _loaded_at = time.time()
    return SOURCE


reload(force=True)


def _public(d: dict) -> dict:
    """SNMP のコミュニティなど、答えに出す必要のない項目を落とす"""
    return {
        "device_id": d["device_id"], "site": d["site"], "role": d["role"], "mgmt_ip": d.get("mgmt_ip"),
        "asn": d.get("asn"), "monitored": bool(d.get("enabled")),
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
    affected = [{"device_id": k, "hops": v, "site": DEVICE_BY_ID[k]["site"], "role": DEVICE_BY_ID[k]["role"]}
                for k, v in seen.items() if k != device_id]
    affected.sort(key=lambda r: (r["hops"], r["device_id"]))
    return {"device_id": device_id, "max_hops": max_hops, "affected": affected}


def topology_graph() -> dict:
    return {"source": SOURCE, "nodes": [_public(d) for d in DEVICES], "links": LINKS}


TOOL_SPECS = [
    {"toolSpec": {
        "name": "list_devices",
        "description": "監視対象ネットワークの機器一覧（拠点 site、役割 role、管理 IP、AS 番号）。site や role で絞れる。",
        "inputSchema": {"json": {"type": "object", "properties": {
            "site": {"type": "string", "description": "拠点名で絞る（hq / dc / br1 / br2 / carrier）。空なら全部"},
            "role": {"type": "string", "description": "役割で絞る（pe / ce / host）。空なら全部"},
        }}},
    }},
    {"toolSpec": {
        "name": "neighbors",
        "description": "機器の隣接（接続先の機器、両端のインタフェース名、回線の種別 ebgp/ibgp/l2、主副、帯域）。",
        "inputSchema": {"json": {"type": "object", "required": ["device_id"], "properties": {
            "device_id": {"type": "string", "description": "機器名（例 hq-ce-01）"},
        }}},
    }},
    {"toolSpec": {
        "name": "blast_radius",
        "description": "機器が停止したときに影響が及ぶ範囲（指定ホップ数以内の機器と拠点）。",
        "inputSchema": {"json": {"type": "object", "required": ["device_id"], "properties": {
            "device_id": {"type": "string", "description": "停止を想定する機器名"},
            "max_hops": {"type": "integer", "description": "何ホップ先まで見るか（既定 2、最大 6）"},
        }}},
    }},
    {"toolSpec": {
        "name": "topology_graph",
        "description": "ネットワーク全体のノードとリンクの一覧。全体像を説明するときに使う。",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }},
]
TOOLS = {"list_devices": list_devices, "neighbors": neighbors, "blast_radius": blast_radius, "topology_graph": topology_graph}


def run_tool(name: str, args: dict) -> dict:
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    reload()
    try:
        return fn(**{k: v for k, v in (args or {}).items() if k in fn.__code__.co_varnames})
    except (TypeError, ValueError) as e:
        return {"error": str(e)}
