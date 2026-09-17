"""Neptune（terraform/graph）に置いたトポロジの読み書き。boto3 の neptunedata で Gremlin を送る（IAM 認証の署名は boto3 が付ける）。

エンドポイントは環境変数 NEPTUNE_ENDPOINT（host:port）、無ければ SSM の <PARAM_PREFIX>/neptune-endpoint（terraform/graph が書く）。
どちらも無ければ configured() が False で、topology.py は data/ の静的データを使う（フェーズ 1 のまま動く）。

グラフの形は data/topology.json と同じ:
  頂点 label=device, id=device_id。property: hostname, site, role, asn, mgmt_ip, enabled, status
  辺   label=link, a → b（a < b）。property: a_if, b_if, kind, role, bandwidth_mbps, status

status（UP / DOWN / ALARM）は動的な状態で、Spark の検知（AnomalyOpened / AnomalyResolved）を受けた graph/status_handler.py（terraform/graph の Lambda）が
set_status() で書く。無ければ UP。seed() で入れ直すと消える（静的な構成だけを入れる）。
"""

import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

PARAM_PREFIX = os.environ.get("PARAM_PREFIX", "")
REGION = os.environ.get("AWS_REGION") or os.environ.get("BEDROCK_REGION") or None
TTL = 60
_cache = {"endpoint": "", "checked": 0.0, "client": None}


def endpoint() -> str:
    env = os.environ.get("NEPTUNE_ENDPOINT", "")
    if env:
        return env
    if _cache["endpoint"] or time.time() - _cache["checked"] < TTL or not PARAM_PREFIX:
        return _cache["endpoint"]
    _cache["checked"] = time.time()
    try:
        _cache["endpoint"] = boto3.client("ssm", region_name=REGION).get_parameter(
            Name=f"{PARAM_PREFIX}/neptune-endpoint")["Parameter"]["Value"]
    except (ClientError, BotoCoreError):
        _cache["endpoint"] = ""
    return _cache["endpoint"]


def configured() -> bool:
    return bool(endpoint())


def _client():
    ep = endpoint()
    if _cache["client"] is None or _cache["client"][0] != ep:
        # 既定（接続 60 秒 × 再試行）だと SG で落とされたときに 1 回の呼び出しが数分かかり、
        # ops/up.sh の 8-2 が何十分も黙る。接続は 10 秒・再試行 1 回で早く諦める
        _cache["client"] = (ep, boto3.client(
            "neptunedata", endpoint_url=f"https://{ep}", region_name=REGION,
            config=Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 2})))
    return _cache["client"][1]


def _un(v):
    """GraphSON 3 の型付き値（{"@type": "g:List", "@value": [...]} など）を素の Python に"""
    if isinstance(v, dict) and "@type" in v:
        t, val = v["@type"], v.get("@value")
        if t == "g:List" or t == "g:Set":
            return [_un(x) for x in val]
        if t == "g:Map":
            it = iter(val)
            return {_un(k): _un(x) for k, x in zip(it, it)}
        return _un(val)
    if isinstance(v, dict):
        return {k: _un(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_un(x) for x in v]
    return v


def query(gremlin: str):
    res = _client().execute_gremlin_query(gremlinQuery=gremlin)
    return _un(res.get("result", {})).get("data", []) if isinstance(res.get("result"), dict) else _un(res.get("result"))


def _q(v) -> str:
    """Gremlin のリテラル。文字列は ' で囲み、None は書かない（呼ぶ側で落とす）"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _props(d: dict, keys) -> str:
    return "".join(f".property({_q(k)},{_q(d[k])})" for k in keys if d.get(k) is not None and d.get(k) != "")


DEVICE_KEYS = ("hostname", "site", "role", "asn", "mgmt_ip", "enabled", "status")
LINK_KEYS = ("a_if", "b_if", "kind", "role", "bandwidth_mbps", "status")
STATUSES = ("UP", "DOWN", "ALARM")


def load_topology() -> tuple[list[dict], list[dict]]:
    """(devices, links)。topology.py が data/ の代わりに使う形（devices は asn を含む）"""
    devices = []
    for m in query("g.V().hasLabel('device').elementMap()"):
        d = {k: m.get(k) for k in DEVICE_KEYS}
        d["device_id"] = m.get("id")
        d["enabled"] = bool(d.get("enabled"))
        devices.append(d)
    links = []
    for m in query("g.E().hasLabel('link').elementMap()"):
        l = {k: m.get(k) for k in LINK_KEYS}
        l["a"], l["b"] = m.get("OUT", {}).get("id"), m.get("IN", {}).get("id")
        links.append(l)
    devices.sort(key=lambda d: d["device_id"])
    links.sort(key=lambda l: (l["a"], l["b"], l["a_if"] or ""))
    return devices, links


def count() -> dict:
    return {"devices": query("g.V().hasLabel('device').count()")[0], "links": query("g.E().hasLabel('link').count()")[0]}


def seed(devices: list[dict], links: list[dict]) -> dict:
    """静的データで置き換える（全部消してから入れる）。devices は devices.yaml の行に topology.json の asn を足したもの、
    または lab/lab_topology.py が lab の定義から作ったもの（同じ形）。status は入れない（入れ直したら全部 UP に戻る）"""
    query("g.V().hasLabel('device').drop()")
    for d in devices:
        query(f"g.addV('device').property(id,{_q(d['device_id'])}){_props(d, DEVICE_KEYS[:-1])}")
    for l in links:
        add_link(l["a"], l["a_if"], l["b"], l["b_if"], l.get("kind") or "l2", l.get("role") or "", l.get("bandwidth_mbps"))
    return count()


def add_device(device_id: str, site: str, role: str, mgmt_ip: str = "", asn=None, enabled: bool = False) -> dict:
    if query(f"g.V({_q(device_id)}).count()")[0]:
        return {"error": f"{device_id} はもうある"}
    d = {"hostname": device_id, "site": site, "role": role, "mgmt_ip": mgmt_ip, "asn": asn, "enabled": enabled}
    query(f"g.addV('device').property(id,{_q(device_id)}){_props(d, DEVICE_KEYS)}")
    return {"added": device_id}


def remove_device(device_id: str) -> dict:
    n = query(f"g.V({_q(device_id)}).count()")[0]
    if not n:
        return {"error": f"{device_id} は無い"}
    query(f"g.V({_q(device_id)}).drop()")  # つながる辺も消える
    return {"removed": device_id}


def add_link(a: str, a_if: str, b: str, b_if: str, kind: str = "l2", role: str = "", bandwidth_mbps=None) -> dict:
    if a == b:
        return {"error": "両端が同じ機器"}
    if a > b:
        a, a_if, b, b_if = b, b_if, a, a_if
    missing = [x for x in (a, b) if not query(f"g.V({_q(x)}).count()")[0]]
    if missing:
        return {"error": f"機器が無い: {', '.join(missing)}"}
    if query(f"g.V({_q(a)}).outE('link').where(inV().hasId({_q(b)})).has('a_if',{_q(a_if)}).count()")[0]:
        return {"error": f"{a} {a_if} - {b} のリンクはもうある"}
    l = {"a_if": a_if, "b_if": b_if, "kind": kind, "role": role, "bandwidth_mbps": int(bandwidth_mbps) if bandwidth_mbps else None}
    query(f"g.addE('link').from(__.V({_q(a)})).to(__.V({_q(b)})){_props(l, LINK_KEYS)}")
    return {"added": f"{a} {a_if} - {b} {b_if}"}


def remove_link(a: str, b: str, a_if: str = "") -> dict:
    if a > b:
        a, b = b, a
    q = f"g.V({_q(a)}).outE('link').where(inV().hasId({_q(b)}))" + (f".has('a_if',{_q(a_if)})" if a_if else "")
    n = query(q + ".count()")[0]
    if not n:
        return {"error": f"{a} - {b} のリンクは無い"}
    query(q + ".drop()")
    return {"removed": n}


def set_status(device_id: str, if_name: str = "", status: str = "DOWN") -> dict:
    """動的な状態を書く。if_name があればその機器のそのインタフェースが付く辺（a 側でも b 側でも）、無ければ機器の頂点。
    戻り値の updated は書いた要素の数（機器やインタフェースがトポロジに無ければ 0。エラーにはしない。検知はトポロジより先に来ることがある）"""
    status = str(status).upper()
    if status not in STATUSES:
        return {"error": f"status は {' / '.join(STATUSES)} のどれか"}
    dev = _q(device_id)
    if if_name:
        n = query(f"g.V({dev}).outE('link').has('a_if',{_q(if_name)}).property('status',{_q(status)}).count()")[0]
        n += query(f"g.V({dev}).inE('link').has('b_if',{_q(if_name)}).property('status',{_q(status)}).count()")[0]
        return {"device_id": device_id, "if_name": if_name, "status": status, "updated": int(n)}
    n = query(f"g.V({dev}).property('status',{_q(status)}).count()")[0]
    return {"device_id": device_id, "status": status, "updated": int(n)}
