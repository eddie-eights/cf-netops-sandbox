"""静的トポロジ（data/devices.yaml と data/topology.json）をエージェントのツールとして出す。

中身はローカル lab（lab/wvs2.clab.yml）の 10 台そのもので、すべて架空のアドレス。
SNMP や lab には触らない。読み取りだけなので、モデルが何度呼んでも副作用は無い。

Converse の toolConfig に渡す仕様（TOOL_SPECS）と、toolUse を受けて実行する run_tool() を持つ。
"""

import json
import os
from collections import deque

import yaml

DATA_DIR = os.environ.get("TOPOLOGY_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
# 段の順（上が上流）。frontend の ROLE_ORDER と同じ
ROLE_ORDER = ["core", "pe", "distribution", "aggregation", "access", "ce", "host"]
MAX_HOPS = 6


def _load():
    with open(os.path.join(DATA_DIR, "devices.yaml"), encoding="utf-8") as f:
        devices = yaml.safe_load(f)["devices"]
    with open(os.path.join(DATA_DIR, "topology.json"), encoding="utf-8") as f:
        topo = json.load(f)
    nodes = {n["device_id"]: n for n in topo["nodes"]}
    # links は a < b で正規化してあるが、探索は無向で見る
    adj: dict[str, list[dict]] = {d["device_id"]: [] for d in devices}
    for l in topo["links"]:
        for me, peer, my_if, peer_if in ((l["a"], l["b"], l["a_if"], l["b_if"]), (l["b"], l["a"], l["b_if"], l["a_if"])):
            adj.setdefault(me, []).append({
                "device_id": peer, "local_if": my_if, "remote_if": peer_if,
                "kind": l["kind"], "role": l.get("role") or "", "bandwidth_mbps": l.get("bandwidth_mbps"),
            })
    return devices, nodes, topo["links"], adj


DEVICES, NODES, LINKS, ADJ = _load()
DEVICE_BY_ID = {d["device_id"]: d for d in DEVICES}


def _public(d: dict) -> dict:
    """SNMP のコミュニティなど、答えに出す必要のない項目を落とす"""
    n = NODES.get(d["device_id"], {})
    return {
        "device_id": d["device_id"], "site": d["site"], "role": d["role"], "mgmt_ip": d["mgmt_ip"],
        "asn": n.get("asn"), "monitored": bool(d.get("enabled")),
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
    return {"nodes": [_public(d) for d in DEVICES], "links": LINKS}


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
    try:
        return fn(**{k: v for k, v in (args or {}).items() if k in fn.__code__.co_varnames})
    except (TypeError, ValueError) as e:
        return {"error": str(e)}
