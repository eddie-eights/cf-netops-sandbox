"""Neptune（terraform/pipeline/graph）に置いたトポロジの読み書き。boto3 の neptunedata で Gremlin を送る（IAM 認証の署名は boto3 が付ける）。

エンドポイントは環境変数 NEPTUNE_ENDPOINT（host:port）、無ければ SSM の <PARAM_PREFIX>/neptune-endpoint（terraform/pipeline/graph が書く）。
どちらも無ければ configured() が False で、topology.py は data/ の静的データを使う（graph を作っていなくても動く）。

グラフの形は data/topology.json に、インタフェースの頂点を足したもの:
  頂点 label=device, id=device_id。property: hostname, site, role, asn, mgmt_ip, enabled, status
  頂点 label=interface, id=<device_id>#<IF 名>。property: device_id, name, address, status（機器とは辺でなく device_id でつなぐ）
  辺   label=link, a → b（a < b）。property: a_if, b_if, kind, role, bandwidth_mbps, status
インタフェースは lab/lab_topology.py が lab の定義から作る全部（管理の mgmt0 やリンクに出ない IF も）。agent/data の静的データには無い。

物理層より上（IP 層 / EVPN・BGP 層。data/layers.json、lab/lab_topology.py の layers）は、機器やインタフェースとは別の頂点で、
下の層の頂点の id を property に持つ（interface_id / ip_interface_id。層をまたぐ紐づけの鍵）:
  頂点 label=ip_interface（id <機器>#<IF>.<n>）、isis_adjacency（<機器>#isis#<IF>.<n>）、bgp_session（<機器>#bgp#<相手の IP>）、
       evpn_instance（<機器>#evi#<EVI>）、ethernet_segment（<機器>#es#<名前>）。property は lab_topology.py の docstring の通り + status + layer（ip / evpn）
  辺   over（上の層 → 下の層の頂点）、peer（IS-IS の隣接 / BGP のセッションの両端）、tunnel（同じ EVI 同士）、attach（EVI → LAG の IF）、segment（同じ ESI 同士）
status は物理層と同じ動的な状態で、gNMI の検知（bgp_down / isis_down）を受けた graph/status_handler.py が set_layer_status() で書く。

status（UP / DOWN / ALARM）は動的な状態で、Grafana / Splunk のアラート（firing / resolved。SNS のトピック）を受けた graph/status_handler.py（terraform/pipeline/graph の Lambda）が
set_status() で書く。無ければ UP。seed() で入れ直すと消える（静的な構成だけを入れる）。
Nautobot を正にしているとき（terraform/pipeline/nautobot）は、Nautobot の Job が sync_physical() で物理層だけを差分で合わせる（status と上の層は残る）。

トポロジに無い機器やインタフェースの異常は捨てずに「未登録」の頂点（property registered=false。機器は role=unknown）として残し、
set_status() の戻り値に unregistered を付ける（Lambda が WARNING でログに出す。登録漏れの印）。登録済みの頂点は registered を持たない。
あとから seed() / add_device() で登録すると未登録の頂点は置き換わり、UP でない status は引き継ぐ。
"""

import boto3
from botocore.config import Config

import toolkit

REGION = toolkit.REGION
ENDPOINT = toolkit.Param("NEPTUNE_ENDPOINT", "neptune-endpoint")  # 接続先（環境変数か SSM）
_cache = {"client": None}


def endpoint() -> str:
    return ENDPOINT.value()


def configured() -> bool:
    """Neptune を配備してあるか（無ければ topology.py は data/ の静的データに戻る）"""
    return bool(endpoint())


def _client():
    """neptunedata のクライアント。接続先が変わらないかぎり作り直さない"""
    ep = endpoint()
    if _cache["client"] is None or _cache["client"][0] != ep:
        # 既定（接続 60 秒 × 再試行）だと SG で落とされたときに 1 回の呼び出しが数分かかり、
        # ops/up.sh の 7-3b が何十分も黙る。接続は 10 秒・再試行 1 回で早く諦める
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


_ESCAPES = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _q(v) -> str:
    """Gremlin のリテラル。文字列は ' で囲み、None は書かない（呼ぶ側で落とす）。
    Neptune の文字列の Gremlin は生の改行や制御文字を受け付けないので \\n / \\uXXXX に直す（修復案の本文や承認者の名前に何が入っても壊れない。
    spark/snmp_sinks.py の gremlin_literal、workflow/awsio.py の _q と同じ）"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + "".join(_ESCAPES.get(ch) or ("\\u%04x" % ord(ch) if ord(ch) < 0x20 or ord(ch) == 0x7F else ch) for ch in str(v)) + "'"


def _props(d: dict, keys) -> str:
    return "".join(f".property({_q(k)},{_q(d[k])})" for k in keys if d.get(k) is not None and d.get(k) != "")


DEVICE_KEYS = ("hostname", "site", "role", "asn", "mgmt_ip", "enabled", "status")
IF_KEYS = ("device_id", "name", "address", "lag", "status")
LINK_KEYS = ("a_if", "b_if", "kind", "role", "bandwidth_mbps", "status")
STATUSES = ("UP", "DOWN", "ALARM")
LAYER_LABELS = ("ip_interface", "isis_adjacency", "bgp_session", "evpn_instance", "ethernet_segment")
LAYER_EDGES = ("over", "peer", "tunnel", "attach", "segment")
LAYER_KIND = {"bgp": "bgp_session", "isis": "isis_adjacency"}   # set_layer_status の kind（id の真ん中）→ label


def _if_id(device_id: str, if_name: str) -> str:
    return f"{device_id}#{if_name}"


def load_topology() -> tuple[list[dict], list[dict]]:
    """(devices, links)。topology.py が data/ の代わりに使う形（devices は asn を含む）。
    devices の各行には interfaces（[{name, address, status, registered}]）と registered（未登録の頂点なら False）が付く"""
    devices = {}
    for m in query("g.V().hasLabel('device').elementMap()"):
        d = {k: m.get(k) for k in DEVICE_KEYS}
        d["device_id"] = m.get("id")
        d["enabled"] = bool(d.get("enabled"))
        d["registered"] = m.get("registered") is not False
        d["interfaces"] = []
        devices[d["device_id"]] = d
    for m in query("g.V().hasLabel('interface').elementMap()"):
        d = devices.get(m.get("device_id"))
        if d is not None:
            d["interfaces"].append({"name": m.get("name"), "address": m.get("address"), "lag": m.get("lag") or "",
                                    "status": m.get("status"), "registered": m.get("registered") is not False})
    links = []
    for m in query("g.E().hasLabel('link').elementMap()"):
        l = {k: m.get(k) for k in LINK_KEYS}
        l["a"], l["b"] = m.get("OUT", {}).get("id"), m.get("IN", {}).get("id")
        links.append(l)
    for d in devices.values():
        d["interfaces"].sort(key=lambda i: str(i["name"] or ""))
    links.sort(key=lambda l: (l["a"], l["b"], l["a_if"] or ""))
    return sorted(devices.values(), key=lambda d: d["device_id"]), links


def _labels(labels) -> str:
    return ",".join(_q(x) for x in labels)


def count() -> dict:
    """登録済みの機器・インタフェース・回線の数、上の層の頂点と辺の数、未登録の頂点の数（ops/seed_graph.py は devices が 0 なら空とみなす）"""
    return {"devices": query("g.V().hasLabel('device').hasNot('registered').count()")[0],
            "interfaces": query("g.V().hasLabel('interface').hasNot('registered').count()")[0],
            "links": query("g.E().hasLabel('link').count()")[0],
            "layers": query(f"g.V().hasLabel({_labels(LAYER_LABELS)}).hasNot('registered').count()")[0],
            "layer_edges": query(f"g.E().hasLabel({_labels(LAYER_EDGES)}).count()")[0],
            "unregistered": query("g.V().has('registered',false).count()")[0]}


def load_layers() -> dict:
    """上の層の頂点と辺（lab_topology.py の layers と同じ形。頂点には status と registered が付く）"""
    vertices = []
    for m in query(f"g.V().hasLabel({_labels(LAYER_LABELS)}).elementMap()"):
        v = {k: x for k, x in m.items() if k != "registered"}
        v["registered"] = m.get("registered") is not False
        vertices.append(v)
    edges = [{"label": m.get("label"), "from": m.get("OUT", {}).get("id"), "to": m.get("IN", {}).get("id")}
             for m in query(f"g.E().hasLabel({_labels(LAYER_EDGES)}).elementMap()")]
    vertices.sort(key=lambda v: str(v.get("id")))
    edges.sort(key=lambda e: (e["label"], str(e["from"]), str(e["to"])))
    return {"vertices": vertices, "edges": edges}


def _add_interfaces(device_id: str, interfaces) -> None:
    for i in interfaces or []:
        if i.get("name"):
            v = {"device_id": device_id, "name": i["name"], "address": i.get("address"), "lag": i.get("lag") or ""}
            query(f"g.addV('interface').property(id,{_q(_if_id(device_id, i['name']))}){_props(v, IF_KEYS[:-1])}")


def seed(devices: list[dict], links: list[dict], layers: dict | None = None) -> dict:
    """静的データで置き換える（登録済みを全部消してから入れる）。devices は lab/lab_topology.py が lab の定義から作ったもの
    （interfaces 付き）、または devices.yaml の行に topology.json の asn を足したもの（interfaces 無し）。layers は同じく lab_topology.py の
    layers（または data/layers.json）で、None なら上の層は触らない。
    status は入れない（入れ直したら全部 UP に戻る）。ただし未登録の頂点のうち今回登録されるものは置き換え、UP でない status を引き継ぐ。
    登録されないままの未登録の頂点は残す（登録漏れの印を入れ直しで消さない）"""
    dev_ids = {d["device_id"] for d in devices}
    if_ids = {_if_id(d["device_id"], i["name"]) for d in devices for i in d.get("interfaces") or [] if i.get("name")}
    carry, replaced = [], []
    for m in query("g.V().has('registered',false).elementMap()"):
        vid = m.get("id")
        if vid in dev_ids and m.get("label") == "device":
            replaced.append(vid)
            carry.append((vid, "", m.get("status")))
        elif vid in if_ids and m.get("label") == "interface":
            replaced.append(vid)
            carry.append((m.get("device_id"), m.get("name"), m.get("status")))
    query("g.V().hasLabel('device','interface').hasNot('registered').drop()")
    for vid in replaced:
        query(f"g.V({_q(vid)}).drop()")
    for d in devices:
        query(f"g.addV('device').property(id,{_q(d['device_id'])}){_props(d, DEVICE_KEYS[:-1])}")
        _add_interfaces(d["device_id"], d.get("interfaces"))
    for l in links:
        add_link(l["a"], l["a_if"], l["b"], l["b_if"], l.get("kind") or "l2", l.get("role") or "", l.get("bandwidth_mbps"))
    for dev, ifn, st in carry:
        if st and st != "UP":
            set_status(dev, ifn, st)
    out = count()
    if layers is not None:
        out.update(seed_layers(layers, if_ids))
    return out


def seed_layers(layers: dict, known_ids: set | None = None) -> dict:
    """上の層（ip_interface / isis_adjacency / bgp_session / evpn_instance / ethernet_segment と、その辺）を置き換える。
    seed() と同じく、未登録の頂点のうち今回登録されるものは UP でない status を引き継ぐ。辺は両端の頂点があるときだけ張り、
    無いものは skipped_edges に数える（known_ids は物理層の頂点の id。None なら Neptune のインタフェースの id を読む）"""
    vertices, edges = layers.get("vertices") or [], layers.get("edges") or []
    ids = {v["id"] for v in vertices if v.get("id")}
    if known_ids is None:
        known_ids = set(query("g.V().hasLabel('interface').id()"))
    carry = {}
    for m in query(f"g.V().hasLabel({_labels(LAYER_LABELS)}).has('registered',false).elementMap()"):
        if m.get("id") in ids:
            carry[m["id"]] = m.get("status")
            query(f"g.V({_q(m['id'])}).drop()")
    query(f"g.V().hasLabel({_labels(LAYER_LABELS)}).hasNot('registered').drop()")
    for v in vertices:
        if not v.get("id") or v.get("label") not in LAYER_LABELS:
            continue
        keys = [k for k in v if k not in ("id", "label", "status", "registered")]
        query(f"g.addV({_q(v['label'])}).property(id,{_q(v['id'])}){_props(v, keys)}")
    skipped = 0
    for e in edges:
        if e.get("label") not in LAYER_EDGES or not ({e.get("from"), e.get("to")} <= (ids | known_ids)):
            skipped += 1
            continue
        query(f"g.addE({_q(e['label'])}).from(__.V({_q(e['from'])})).to(__.V({_q(e['to'])}))")
    for vid, st in carry.items():
        if st and st != "UP":
            query(f"g.V({_q(vid)}).property(single,'status',{_q(st)})")
    out = {k: v for k, v in count().items() if k in ("layers", "layer_edges", "unregistered")}
    if skipped:
        out["skipped_edges"] = skipped
    return out


def _diff_props(cur: dict, new: dict, keys, single: bool = True) -> str:
    """cur（elementMap の 1 件）を new に合わせる Gremlin の続き。変わった値は上書き（頂点は single。辺に cardinality は無い）、
    無くなった値（None / ""）は property ごと drop。同じなら空文字"""
    out = ""
    for k in keys:
        v = new.get(k)
        if v is None or v == "":
            if cur.get(k) is not None:
                out += f".sideEffect(properties({_q(k)}).drop())"
        elif cur.get(k) != v:
            out += f".property({'single,' if single else ''}{_q(k)},{_q(v)})"
    return out


def sync_physical(devices: list[dict], links: list[dict]) -> dict:
    """物理層（機器・インタフェース・回線）を、渡した一覧に差分で合わせる。Nautobot の Job（nautobot/jobs）が、Nautobot を変えるたびに呼ぶ。
    devices / links の形は seed() と同じ。seed() と違って消して入れ直さないので、残る頂点と辺の status（アラートが付けた動的な状態）と、
    上の層（IP 層 / EVPN・BGP 層。lab の定義から seed_layers() で入れたもの）の頂点と辺はそのまま残る。
      - 一覧に無い登録済みの機器・インタフェース・回線は消す（機器を消すと付いていた辺も消える）
      - 一覧にあって Neptune に無いものは足す。未登録の頂点（検知が先に来たもの）は置き換え、UP でない status を引き継ぐ（seed() と同じ）
      - どちらにもあるものは、変わった property だけ single で上書きし、無くなった値は property ごと消す
    一覧に入らない未登録の頂点は残す。戻り値は count() に added / updated / removed（書いた要素の数）を足したもの。
    回線の端の機器が一覧に無いなどで張れなかった回線は skipped に理由を並べる"""
    want = {"device": {d["device_id"]: d for d in devices}, "interface": {}}
    for d in devices:
        for i in d.get("interfaces") or []:
            if i.get("name"):
                want["interface"][_if_id(d["device_id"], i["name"])] = {
                    "device_id": d["device_id"], "name": i["name"], "address": i.get("address"), "lag": i.get("lag") or ""}
    want_links = {}
    for l in links:
        a, a_if, b, b_if = l["a"], l["a_if"], l["b"], l["b_if"]
        if a > b:
            a, a_if, b, b_if = b, b_if, a, a_if
        want_links[(a, b, a_if)] = {"b_if": b_if, "kind": l.get("kind") or "l2", "role": l.get("role") or "",
                                    "bandwidth_mbps": int(l["bandwidth_mbps"]) if l.get("bandwidth_mbps") else None}
    current = {m.get("id"): m for m in query("g.V().hasLabel('device','interface').elementMap()")}
    edges = query("g.E().hasLabel('link').elementMap()")
    stats = {"added": 0, "updated": 0, "removed": 0}
    gone, carry = set(), []   # gone = 消した / 作り直した機器（付いていた辺も一緒に消えている）
    for label, keys in (("device", DEVICE_KEYS[:-1]), ("interface", IF_KEYS[:-1])):
        for vid, m in current.items():
            if m.get("label") == label and vid not in want[label] and m.get("registered") is not False:
                query(f"g.V({_q(vid)}).drop()")
                stats["removed"] += 1
                if label == "device":
                    gone.add(vid)
        for vid, w in want[label].items():
            m = current.get(vid)
            if m is not None and m.get("label") == label and m.get("registered") is not False:
                diff = _diff_props(m, w, keys)
                if diff:
                    query(f"g.V({_q(vid)}){diff}.id()")
                    stats["updated"] += 1
                continue
            if m is not None:   # 未登録の頂点は置き換える
                query(f"g.V({_q(vid)}).drop()")
                carry.append((vid, "", m.get("status")) if label == "device" else (w["device_id"], w["name"], m.get("status")))
                if label == "device":
                    gone.add(vid)
            query(f"g.addV({_q(label)}).property(id,{_q(vid)}){_props(w, keys)}")
            stats["added"] += 1
    kept = set()
    for m in edges:
        a, b = m.get("OUT", {}).get("id"), m.get("IN", {}).get("id")
        if a in gone or b in gone:
            continue
        key = (a, b, m.get("a_if"))
        if key not in want_links or key in kept:   # 一覧に無い回線と、同じ回線の 2 本目
            query(f"g.E({_q(m.get('id'))}).drop()")
            stats["removed"] += 1
            continue
        kept.add(key)
        diff = _diff_props(m, want_links[key], LINK_KEYS[1:-1], single=False)
        if diff:
            query(f"g.E({_q(m.get('id'))}){diff}.id()")
            stats["updated"] += 1
    skipped = []
    for (a, b, a_if), w in want_links.items():
        if (a, b, a_if) in kept:
            continue
        r = add_link(a, a_if, b, w["b_if"], w["kind"], w["role"], w["bandwidth_mbps"])
        if "error" in r:
            skipped.append(r["error"])
        else:
            stats["added"] += 1
    for dev, ifn, st in carry:
        if st and st != "UP":
            set_status(dev, ifn, st)
    return {**count(), **stats, **({"skipped": skipped} if skipped else {})}


def _registered(vid: str) -> list:
    """[] = 無い、[True] = 登録済み、[False] = 未登録の頂点"""
    return query(f"g.V({_q(vid)}).coalesce(values('registered'),constant(true))")


def add_device(device_id: str, site: str, role: str, mgmt_ip: str = "", asn=None, enabled: bool = False) -> dict:
    """機器を足す。未登録の頂点（検知が先に来たもの）があれば置き換え、その status を引き継ぐ"""
    reg = _registered(device_id)
    if reg and reg[0] is not False:
        return {"error": f"{device_id} はもうある"}
    status = None
    if reg:
        status = (query(f"g.V({_q(device_id)}).values('status')") or [None])[0]
        query(f"g.V({_q(device_id)}).drop()")
    d = {"hostname": device_id, "site": site, "role": role, "mgmt_ip": mgmt_ip, "asn": asn, "enabled": enabled, "status": status}
    query(f"g.addV('device').property(id,{_q(device_id)}){_props(d, DEVICE_KEYS)}")
    return {"added": device_id, **({"replaced_unregistered": True} if reg else {})}


def remove_device(device_id: str) -> dict:
    n = query(f"g.V({_q(device_id)}).count()")[0]
    if not n:
        return {"error": f"{device_id} は無い"}
    query(f"g.V({_q(device_id)}).drop()")  # つながる辺も消える
    query(f"g.V().hasLabel('interface').has('device_id',{_q(device_id)}).drop()")  # インタフェースは辺でつないでいないので別に消す
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


def _upsert_unregistered(vid: str, label: str, props: dict) -> None:
    """未登録の頂点を作る（あれば何もしない）。Lambda が同時に 2 つ動いても同じ id を 2 回 addV しないよう coalesce で"""
    query(f"g.V({_q(vid)}).fold().coalesce(unfold(),addV({_q(label)}).property(id,{_q(vid)}){_props(props, props)}"
          ".property('registered',false))")


def set_status(device_id: str, if_name: str = "", status: str = "DOWN", only_if: str = "") -> dict:
    """動的な状態を書く。if_name があればその機器のそのインタフェースが付く辺（a 側でも b 側でも）とインタフェースの頂点、無ければ機器の頂点。
    戻り値の updated は書いた要素の数。トポロジに無ければ未登録の頂点を作って unregistered: True を返す（UP に戻すだけのときは作らない）。
    未登録の頂点に書いたときも unregistered: True。頂点の property は single で書く（Neptune の既定は set で、値が積み重なる）。
    only_if（機器の頂点だけ）を渡すと、今の status がそれのときだけ書く（trap の解消で ALARM を UP に戻すとき、
    IF の分からない linkDown が付けた DOWN まで UP に上書きしないため）。合わなければ updated: 0 で何も作らない"""
    status = str(status).upper()
    if status not in STATUSES:
        return {"error": f"status は {' / '.join(STATUSES)} のどれか"}
    dev, st = _q(device_id), _q(status)
    if if_name:
        n = query(f"g.V({dev}).outE('link').has('a_if',{_q(if_name)}).property('status',{st}).count()")[0]
        n += query(f"g.V({dev}).inE('link').has('b_if',{_q(if_name)}).property('status',{st}).count()")[0]
        reg = query(f"g.V({_q(_if_id(device_id, if_name))}).property(single,'status',{st}).coalesce(values('registered'),constant(true))")
        out = {"device_id": device_id, "if_name": if_name, "status": status, "updated": int(n) + len(reg)}
        if not n and not reg and status != "UP":
            _upsert_unregistered(device_id, "device", {"hostname": device_id, "site": "?", "role": "unknown", "enabled": False})
            _upsert_unregistered(_if_id(device_id, if_name), "interface", {"device_id": device_id, "name": if_name})
            query(f"g.V({_q(_if_id(device_id, if_name))}).property(single,'status',{st})")
            out["unregistered"] = True
        elif any(r is False for r in reg):
            out["unregistered"] = True
        return out
    cond = f".has('status',{_q(str(only_if).upper())})" if only_if else ""
    reg = query(f"g.V({dev}){cond}.property(single,'status',{st}).coalesce(values('registered'),constant(true))")
    out = {"device_id": device_id, "status": status, "updated": len(reg)}
    if not reg and status != "UP" and not only_if:
        _upsert_unregistered(device_id, "device", {"hostname": device_id, "site": "?", "role": "unknown", "enabled": False})
        query(f"g.V({dev}).property(single,'status',{st})")
        out["unregistered"] = True
    elif any(r is False for r in reg):
        out["unregistered"] = True
    return out


def set_layer_status(device_id: str, kind: str, target: str, status: str = "DOWN") -> dict:
    """上の層の動的な状態を書く。kind は bgp（target = 相手の IP）か isis（target = サブインタフェース ethernet-1/1.0）で、
    頂点の id は <機器>#<kind>#<target>。無ければ（トポロジに無いセッション）未登録の頂点を作って unregistered: True を返す
    （UP に戻すだけのときは作らない）。set_status と同じく property は single で書く"""
    status = str(status).upper()
    if status not in STATUSES:
        return {"error": f"status は {' / '.join(STATUSES)} のどれか"}
    label = LAYER_KIND.get(str(kind))
    if not label:
        return {"error": f"kind は {' / '.join(LAYER_KIND)} のどれか"}
    vid = f"{device_id}#{kind}#{target}"
    reg = query(f"g.V({_q(vid)}).hasLabel({_q(label)}).property(single,'status',{_q(status)}).coalesce(values('registered'),constant(true))")
    out = {"device_id": device_id, "kind": kind, "target": target, "status": status, "updated": len(reg)}
    if not reg and status != "UP":
        props = {"device_id": device_id, "layer": "evpn" if kind == "bgp" else "ip",
                 ("peer_address" if kind == "bgp" else "name"): target}
        _upsert_unregistered(vid, label, props)
        query(f"g.V({_q(vid)}).property(single,'status',{_q(status)})")
        out["unregistered"] = True
    elif any(r is False for r in reg):
        out["unregistered"] = True
    return out


# ---------------------------------------------------------------- 修復案の頂点（2026-09-24 に DynamoDB から移した）
# 修復案（label proposal）は terraform/workflow のワーカー（workflow/awsio.py）が書く。トポロジの頂点とは辺でつながず、device_id で引く。
# ここは読むのと、承認タブの decide だけ（agent/proposals.py）。異常（label anomaly）の頂点は 2026-10-02 にやめた（書いていた Spark の detect をなくした）
def _record(m: dict, id_key: str) -> dict:
    """elementMap() の 1 件を、id を id_key（anomaly_id / proposal_id）に置き換えた dict に"""
    d = {k: v for k, v in m.items() if k not in ("id", "label")}
    d[id_key] = m.get("id")
    return d


def list_records(label: str, id_key: str, order_by: str, status: str = "", device_id: str = "", limit: int = 20) -> list:
    """label の頂点を order_by（epoch 秒）の新しい順に limit 件。status / device_id があればその値だけ（絞ってから数える）"""
    q = f"g.V().hasLabel({_q(label)})"
    if status:
        q += f".has('status',{_q(status)})"
    if device_id:
        q += f".has('device_id',{_q(device_id)})"
    return [_record(m, id_key) for m in query(f"{q}.order().by({_q(order_by)},desc).limit({int(limit)}).elementMap()")]


def get_record(label: str, id_key: str, vid: str) -> dict:
    rows = query(f"g.V({_q(vid)}).hasLabel({_q(label)}).elementMap()")
    return _record(rows[0], id_key) if rows else {}


def update_record(label: str, vid: str, fields: dict, only_status: str = "") -> bool:
    """頂点の fields を書き換える（property は single）。only_status なら今の status がそれのときだけ。書けたら True。
    has と property が 1 本の Gremlin なので、読んでから書くあいだに別の書き手が割り込まない"""
    cond = f".has('status',{_q(only_status)})" if only_status else ""
    props = "".join(f".property(single,{_q(k)},{_q(v)})" for k, v in fields.items() if v is not None and v != "")
    return bool(query(f"g.V({_q(vid)}).hasLabel({_q(label)}){cond}{props}.id()"))
