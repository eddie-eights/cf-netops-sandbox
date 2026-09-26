"""lab の定義（containerlab のトポロジ + SR Linux の設定）から、Neptune に入れるトポロジ（機器と回線と、IP 層 / EVPN・BGP 層）を作る。

設計の「静的なトポロジ構成の取得・同期（初期 & 定期ロード）」の PoC 版。実機なら LLDP / BGP / NETCONF / gNMI で取るところを、
PoC では機器の定義そのもの（splab.clab.yml.in の nodes / links と srlinux/<機器>.cli。どちらも lab/gen_lab.py が作る）から取る。
出す形は agent/data（devices.yaml + topology.json + layers.json）と同じで、graph.seed() にそのまま渡せる。agent/data と同じ 8 台・12 本になることは
tests/test_sync.py が確かめる（lab を変えて agent/data を直し忘れるとテストが落ちる）。

読むもの:
  - nodes: 名前 <拠点>-<役割>-<連番>（拠点と役割は名前から。group は使わない。役割は leafsw / spine / leaf / upstream / host）、mgmt-ipv4 → mgmt_ip、
    srlinux/<機器>.cli に `set / system snmp` がある機器 → enabled（SNMP と gNMI の監視対象。スイッチ 6 台全部）
  - links: endpoints ["x:e1-N", "y:ethM"] → 回線（a < b に正規化）。SR Linux の e1-N は設定と ifTable（ifName）の ethernet-1/N に読み替える
  - srlinux/<機器>.cli: `protocols bgp autonomous-system <ASN>` → asn、`interface ethernet-1/N description` → 主/副（primary / secondary）と
    帯域（… 1G / 100M / 25G）、`subinterface 0 ipv4 address` → そのインタフェースのアドレス、`system name host-name` → 別名、
    `ethernet aggregate-id lagN` → そのインタフェースが入る LAG（lag）
  - nodes の exec の `ip addr add <アドレス>/<長さ> dev ethN`（VM 側）→ そのインタフェースのアドレス、`ip link set ethN master bond0` → lag
  - 回線の種別: spine が付くなら fabric（IS-IS の p2p）、VM が付いて LAG に入る IF なら lag、それ以外は l2

物理層より上（layers。--layers か JSON の "layers"）は同じ .cli から作る。頂点の id は「機器#種類#対象」で、下の層の id を property に持つ（層をまたぐ紐づけの鍵）:
  - ip_interface   <機器>#<IF>.<n>        interface_id（物理 IF の頂点）、address / prefix_length / network_instance          辺 over → interface
  - isis_adjacency <機器>#isis#<IF>.<n>   ip_interface_id、instance、peer_device                                            辺 over → ip_interface、peer ↔ 相手側
  - bgp_session    <機器>#bgp#<相手の IP>  ip_interface_id（ループバック system0.0）、peer_device、group、afi、asn / peer_as  辺 over → ip_interface、peer ↔ 相手側
  - evpn_instance  <機器>#evi#<EVI>       ip_interface_id（VTEP）、network_instance、vni、route_target、interfaces           辺 over → ip_interface、attach → interface（LAG）、tunnel ↔ 同じ EVI
  - ethernet_segment <機器>#es#<名前>     interface_id（LAG）、esi、mode                                                     辺 over → interface、segment ↔ 同じ ESI
  頂点の property layer は ip（ip_interface / isis_adjacency）か evpn（bgp_session / evpn_instance / ethernet_segment）。
  検知は bgp_session と isis_adjacency の status に書く（graph/status_handler.py の bgp_down / isis_down → graph.set_layer_status）。

機器ごとに、回線の端だけでなく機器が持つインタフェースを全部（interfaces。containerlab の管理 IF（SR Linux は mgmt0、VM は eth0）、
SR Linux の interface（lag1 も）、exec でアドレスを振る IF。system0 / lo0 は除く）と、機器を指す別名（aliases。device_id / hostname / 管理 IP /
全インタフェースとループバックのアドレス。小文字）を付ける。
検知（Spark）とトポロジ（Neptune）で機器とインタフェースの名前が合わずに異常がどこにも付かない、を減らすため。
機器の一覧はここ（lab の定義）1 か所にし、Telegraf のポーリング先と gNMI の接続先、Spark の検知の device map もここから作る（ops/up.sh）。

使い方: python3 lab/lab_topology.py [lab のディレクトリ]  → JSON（{"devices": [...], "links": [...], "layers": {...}}）を標準出力に出す
        --device-map    Spark の検知の --device-map（別名=device_id,...。device_id と同じ別名は省く）を出す
        --snmp-agents   Telegraf の inputs.snmp の agents（監視対象の管理 IP。"udp://<IP>:161", ... の形）を出す
        --gnmi-targets  Telegraf の inputs.gnmi の addresses（監視対象の管理 IP。"<IP>:57400", ... の形）を出す
        --layers        物理層より上（layers）だけを JSON で出す
PyYAML があればそれで読み、無ければ（ops/up.sh を打つ PC の python3）この形の YAML だけ読める小さな読み取りで代える。
"""

import json
import os
import re
import sys

BANDWIDTH_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*([GM])\b")
EXEC_ADDR_RE = re.compile(r"^ip addr(?:ess)? add (\S+?)(?:/\d+)? dev (\S+)")
EXEC_MASTER_RE = re.compile(r"^ip link set (\S+) master (\S+)")   # VM の bond（LACP）のメンバー
MGMT_IF = {"nokia_srlinux": "mgmt0"}   # containerlab が管理ネットワークにつなぐ IF（kind ごと。それ以外は eth0）
MGMT_IF_DEFAULT = "eth0"
VM_ROLES = {"host", "upstream"}   # スイッチでない（linux kind の）ノードの役割
SNMP_PORT = 161       # SR Linux の SNMP サーバ（containerlab が network-instance mgmt で有効にする）
GNMI_PORT = 57400     # SR Linux の gNMI サーバ（同じく containerlab が有効にする。TLS、admin / NokiaSrl1!）
TOPO_FILE = "splab.clab.yml.in"
LOOPBACK_PREFIXES = ("lo", "system")   # インタフェースには数えない（別名にだけ入れる）
SRL_IF_RE = re.compile(r"^e(\d+)-(\d+)(?:-(\d+))?$")   # containerlab の endpoints の e1-2 / e1-2-1


def clab_if(kind: str, name: str) -> str:
    """endpoints の IF 名を機器の設定と ifTable の名前にする。nokia_srlinux の e1-2 → ethernet-1/2（他の kind はそのまま）"""
    m = SRL_IF_RE.match(name) if kind == "nokia_srlinux" else None
    if not m:
        return name
    out = f"ethernet-{m.group(1)}/{m.group(2)}"
    return out + (f"/{m.group(3)}" if m.group(3) else "")


# ---------------------------------------------------------------- YAML（PyYAML が無いときの代わり）
def _scalar(s: str):
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        return [_scalar(x) for x in s[1:-1].split(",") if x.strip()]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _strip_comment(line: str) -> str:
    out, quote = [], ""
    for ch in line:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (not out or out[-1] == " "):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _parse_block(lines: list, i: int, indent: int):
    """lines[i:] の、indent の深さにある 1 つのブロック（マッピングか並び）を読む。戻り値は (値, 次の行番号)"""
    if i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
        seq = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            ind, text = lines[i]
            item = text[2:]
            if ":" in item and not item.lstrip().startswith("["):   # "- key: value" で始まるマッピング
                lines[i] = (ind + 2, item)
                val, i = _parse_block(lines, i, ind + 2)
                seq.append(val)
            else:
                seq.append(_scalar(item))
                i += 1
        return seq, i
    mapping = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        ind, text = lines[i]
        key, _, rest = text.partition(":")
        key = _scalar(key)
        if rest.strip():
            mapping[key] = _scalar(rest)
            i += 1
        else:
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                mapping[key], i = _parse_block(lines, i, lines[i][0])
            elif i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                mapping[key], i = _parse_block(lines, i, indent)   # 同じ深さから始まる並び（"key:\n- a" の書き方）
            else:
                mapping[key] = None
    return mapping, i


def load_yaml(text: str):
    try:
        import yaml  # noqa: PLC0415

        return yaml.safe_load(text)
    except ImportError:
        pass
    lines = []
    for raw in text.splitlines():
        line = _strip_comment(raw.replace("\t", "  "))
        if line.strip():
            lines.append((len(line) - len(line.lstrip(" ")), line.strip()))
    value, _ = _parse_block(lines, 0, lines[0][0] if lines else 0)
    return value


# ---------------------------------------------------------------- SR Linux（`set /` の行だけの CLI）
SRL_SET_RE = re.compile(r"^set / (.+)$")


def parse_srl(text: str) -> dict:
    """{"asn": int | None, "interfaces": {"ethernet-1/1": {"description": "...", "address": "172.16.0.5", "lag": "lag1"}}, "snmp": bool,
    "subinterfaces": {"ethernet-1/1.0": {"interface", "address", "prefix_length", "network_instance"}},
    "isis": {"instance": "main", "interfaces": {"ethernet-1/1.0": {"passive": bool}}},
    "bgp": {"router_id", "groups": {名前: {"peer_as", "local_address", "rr_client"}}, "neighbors": {IP: {"group", "description"}}},
    "evpn": {"macvrf-100": {"evi", "vxlan_interface", "vni", "route_target", "interfaces": [...]}}, "vxlan": {"vxlan1.100": 100},
    "es": {"ES-1": {"esi", "mode", "interface"}}}（`system name host-name` があれば "hostname" も）。
    読むのは `set / ...` の行だけ（コメントと他の行は無視）。無いものは空（asn は None）"""
    asn, ifaces, hostname, snmp = None, {}, None, False
    subifs, ni_of = {}, {}
    isis = {"instance": None, "interfaces": {}}
    bgp = {"router_id": None, "groups": {}, "neighbors": {}}
    evpn, vxlan, es = {}, {}, {}
    for raw in text.splitlines():
        m = SRL_SET_RE.match(raw.strip())
        if not m:
            continue
        line = m.group(1)
        m = re.match(r"^system name host-name (\S+)$", line)
        if m:
            hostname = m.group(1)
            continue
        m = re.match(r'^interface (\S+) description "?(.*?)"?$', line)
        if m:
            ifaces.setdefault(m.group(1), {})["description"] = m.group(2).strip()
            continue
        m = re.match(r"^interface (\S+) subinterface (\d+) ipv4 address (\S+?)(?:/(\d+))?$", line)
        if m:
            ifaces.setdefault(m.group(1), {}).setdefault("address", m.group(3))
            subifs[f"{m.group(1)}.{m.group(2)}"] = {"interface": m.group(1), "address": m.group(3),
                                                    "prefix_length": int(m.group(4)) if m.group(4) else None}
            continue
        m = re.match(r"^interface (\S+) ethernet aggregate-id (\S+)$", line)
        if m:
            ifaces.setdefault(m.group(1), {})["lag"] = m.group(2)
            ifaces.setdefault(m.group(2), {})
            continue
        m = re.match(r"^interface (\S+) ", line)
        if m:
            ifaces.setdefault(m.group(1), {})
            continue
        m = re.match(r"^tunnel-interface (\S+) vxlan-interface (\d+) ingress vni (\d+)$", line)
        if m:
            vxlan[f"{m.group(1)}.{m.group(2)}"] = int(m.group(3))
            continue
        m = re.match(r"^system network-instance protocols evpn ethernet-segments bgp-instance \d+ ethernet-segment (\S+) (\S+) (\S+)$", line)
        if m:
            key = {"esi": "esi", "multi-homing-mode": "mode", "interface": "interface"}.get(m.group(2))
            if key:
                es.setdefault(m.group(1), {})[key] = m.group(3)
            continue
        m = re.match(r"^network-instance (\S+) (.*)$", line)
        if not m:
            if line.startswith("system snmp "):
                snmp = True
            continue
        ni, rest = m.group(1), m.group(2)
        m = re.match(r"^type (\S+)$", rest)
        if m:
            if m.group(1) == "mac-vrf":
                evpn.setdefault(ni, {"interfaces": []})
            continue
        m = re.match(r"^interface (\S+)$", rest)
        if m:
            ni_of[m.group(1)] = ni
            if ni in evpn:
                evpn[ni]["interfaces"].append(m.group(1))
            continue
        m = re.match(r"^vxlan-interface (\S+)$", rest)
        if m:
            evpn.setdefault(ni, {"interfaces": []})["vxlan_interface"] = m.group(1)
            continue
        m = re.match(r"^protocols bgp-evpn bgp-instance \d+ evi (\d+)$", rest)
        if m:
            evpn.setdefault(ni, {"interfaces": []})["evi"] = int(m.group(1))
            continue
        m = re.match(r"^protocols bgp-vpn bgp-instance \d+ route-target export-rt (\S+)$", rest)
        if m:
            evpn.setdefault(ni, {"interfaces": []})["route_target"] = m.group(1)
            continue
        m = re.match(r"^protocols isis instance (\S+) interface (\S+) (passive true|circuit-type \S+|ipv4-unicast .*)$", rest)
        if m:
            isis["instance"] = isis["instance"] or m.group(1)
            e = isis["interfaces"].setdefault(m.group(2), {"passive": False})
            if m.group(3) == "passive true":
                e["passive"] = True
            continue
        m = re.match(r"^protocols bgp autonomous-system (\d+)$", rest)
        if m:
            asn = int(m.group(1))
            continue
        m = re.match(r"^protocols bgp router-id (\S+)$", rest)
        if m:
            bgp["router_id"] = m.group(1)
            continue
        m = re.match(r"^protocols bgp group (\S+) (peer-as (\d+)|transport local-address (\S+)|route-reflector client true)$", rest)
        if m:
            g = bgp["groups"].setdefault(m.group(1), {"peer_as": None, "local_address": None, "rr_client": False})
            if m.group(3):
                g["peer_as"] = int(m.group(3))
            elif m.group(4):
                g["local_address"] = m.group(4)
            else:
                g["rr_client"] = True
            continue
        m = re.match(r'^protocols bgp neighbor (\S+) (peer-group (\S+)|description "?(.*?)"?)$', rest)
        if m:
            n = bgp["neighbors"].setdefault(m.group(1), {"group": None, "description": ""})
            if m.group(3):
                n["group"] = m.group(3)
            else:
                n["description"] = m.group(4).strip()
            continue
    for name, sub in subifs.items():
        sub["network_instance"] = ni_of.get(name)
    for ni, e in evpn.items():
        e["vni"] = vxlan.get(e.get("vxlan_interface") or "")
    out = {"asn": asn, "interfaces": ifaces, "snmp": snmp, "subinterfaces": subifs, "isis": isis, "bgp": bgp,
           "evpn": evpn, "vxlan": vxlan, "es": es}
    if hostname:
        out["hostname"] = hostname
    return out


def bandwidth_mbps(description: str):
    m = BANDWIDTH_RE.search(description or "")
    if not m:
        return None
    n = float(m.group(1))
    return int(n * 1000) if m.group(2) == "G" else int(n)


def link_role(description: str):
    d = (description or "").lower()
    if "secondary" in d:
        return "secondary"
    if "primary" in d:
        return "primary"
    return None


# ---------------------------------------------------------------- 組み立て
def split_name(name: str) -> tuple[str, str]:
    """<拠点>-<役割>-<連番> → (拠点, 役割)。dc1-leafsw-01 → (dc1, leafsw)、wan-upstream-01 → (wan, upstream)"""
    parts = name.split("-")
    if len(parts) < 3:
        raise ValueError(f"機器名が <拠点>-<役割>-<連番> の形でない: {name}")
    return "-".join(parts[:-2]), parts[-2]


def _if_key(name: str):
    """管理 IF（mgmt0 / eth0）を先頭に、あとは ethernet-1/2 < ethernet-1/10 の順に並べる"""
    m = re.match(r"^(.*?)(\d+)$", name)
    mgmt = 0 if name in (MGMT_IF_DEFAULT, *MGMT_IF.values()) else 1
    return (mgmt, m.group(1), int(m.group(2)), "") if m else (mgmt, name, -1, name)


def _inventory(d: dict, spec: dict, cfg: dict, link_ifs: set) -> None:
    """d に interfaces（[{"name", "address", "lag"}]）と aliases（小文字の別名）を足す。cfg は parse_srl の結果（VM は空）"""
    addr, lag = {}, {}
    if d["mgmt_ip"]:
        addr[MGMT_IF.get(str(spec.get("kind") or ""), MGMT_IF_DEFAULT)] = d["mgmt_ip"]
    extra = []   # ループバック（system0 / lo0）のアドレス（インタフェースには数えないが、trap や BGP の送り元になりうるので別名に入れる）
    for name, f in (cfg.get("interfaces") or {}).items():
        if name.startswith(LOOPBACK_PREFIXES):
            extra.append(f.get("address") or "")
        else:
            addr[name] = f.get("address") or addr.get(name, "")
            if f.get("lag"):
                lag[name] = f["lag"]
    for cmd in spec.get("exec") or []:
        m = EXEC_ADDR_RE.match(str(cmd).strip())
        if m:
            addr[m.group(2)] = m.group(1)
        m = EXEC_MASTER_RE.match(str(cmd).strip())
        if m:
            lag[m.group(1)] = m.group(2)
            addr.setdefault(m.group(2), "")
    for name in link_ifs:
        addr.setdefault(name, "")
    d["interfaces"] = [{"name": n, "address": addr[n], "lag": lag.get(n, "")} for n in sorted(addr, key=_if_key)]
    names = [d["device_id"], d["hostname"], cfg.get("hostname") or "", d["mgmt_ip"], *addr.values(), *extra]
    d["aliases"] = sorted({str(x).strip().lower() for x in names if x})


def build(topo: dict, cfg: dict) -> tuple[list[dict], list[dict]]:
    """topo は containerlab の YAML（辞書）、cfg は {機器名: parse_srl の結果}。戻り値は (devices, links)"""
    nodes = topo["topology"]["nodes"]
    devices = []
    for name, spec in nodes.items():
        spec = spec or {}
        site, role = split_name(name)
        devices.append({"device_id": name, "hostname": name, "site": site, "role": role,
                        "mgmt_ip": spec.get("mgmt-ipv4") or "", "asn": cfg.get(name, {}).get("asn"),
                        "enabled": bool(cfg.get(name, {}).get("snmp"))})   # SNMP の設定を持つ機器だけ監視対象（スイッチ全部）
    kind_of = {name: str((spec or {}).get("kind") or "") for name, spec in nodes.items()}

    def endpoint(end: str) -> tuple[str, str]:
        dev, _, ifn = str(end).partition(":")
        if dev not in kind_of:
            raise ValueError(f"links の {dev} が nodes に無い")
        return dev, clab_if(kind_of[dev], ifn)

    link_ifs = {}
    for item in topo["topology"].get("links") or []:
        for end in item["endpoints"]:
            dev, ifn = endpoint(end)
            link_ifs.setdefault(dev, set()).add(ifn)
    for d in devices:
        _inventory(d, nodes[d["device_id"]] or {}, cfg.get(d["device_id"], {}), link_ifs.get(d["device_id"], set()))
    role_of = {d["device_id"]: d["role"] for d in devices}
    links = []
    for item in topo["topology"].get("links") or []:
        ends = item["endpoints"]
        (a, a_if), (b, b_if) = endpoint(ends[0]), endpoint(ends[1])
        if a > b:
            a, a_if, b, b_if = b, b_if, a, a_if
        roles = {role_of[a], role_of[b]}
        if_of = {d["device_id"]: {i["name"]: i for i in d["interfaces"]} for d in devices}
        if roles & VM_ROLES:
            # VM の回線。スイッチ側が LAG に入る（ethernet aggregate-id）か VM 側が bond のメンバーなら lag、そうでなければただの l2
            kind = "lag" if (if_of[a].get(a_if, {}).get("lag") or if_of[b].get(b_if, {}).get("lag")) else "l2"
        elif "spine" in roles:
            kind = "fabric"
        else:
            kind = "l2"
        descs = [cfg.get(a, {}).get("interfaces", {}).get(a_if, {}).get("description", ""),
                 cfg.get(b, {}).get("interfaces", {}).get(b_if, {}).get("description", "")]
        role = next((r for r in map(link_role, descs) if r), None)   # description に primary / secondary があるときだけ
        bws = [bw for bw in map(bandwidth_mbps, descs) if bw]
        links.append({"a": a, "a_if": a_if, "b": b, "b_if": b_if, "kind": kind, "role": role,
                      "bandwidth_mbps": min(bws) if bws else None})
    devices.sort(key=lambda d: d["device_id"])
    links.sort(key=lambda l: (l["a"], l["b"], l["a_if"]))
    return devices, links


def device_map(devices: list[dict]) -> str:
    """Spark の検知の --device-map（別名=device_id,...）。device_id そのものは省く（Spark は sysName をそのまま使う）。
    1 つの別名が 2 台を指していたら止める（どちらの機器か決まらない）"""
    owner = {}
    for d in devices:
        for a in d.get("aliases") or []:
            if owner.setdefault(a, d["device_id"]) != d["device_id"]:
                raise ValueError(f"別名 {a} が {owner[a]} と {d['device_id']} の両方にある")
    return ",".join(f"{a}={dev}" for a, dev in sorted(owner.items()) if a != dev)


def snmp_agents(devices: list[dict]) -> str:
    """Telegraf の inputs.snmp の agents の中身（監視対象 = SNMP の設定を持つ機器の管理 IP）"""
    return ", ".join(f'"udp://{d["mgmt_ip"]}:{SNMP_PORT}"' for d in devices if d.get("enabled") and d.get("mgmt_ip"))


def gnmi_targets(devices: list[dict]) -> str:
    """Telegraf の inputs.gnmi の addresses の中身（同じ監視対象の管理 IP と gNMI のポート）"""
    return ", ".join(f'"{d["mgmt_ip"]}:{GNMI_PORT}"' for d in devices if d.get("enabled") and d.get("mgmt_ip"))


# ---------------------------------------------------------------- 物理層より上（IP 層 / EVPN・BGP 層）
def layers(devices: list[dict], links: list[dict], cfg: dict) -> dict:
    """{"vertices": [{"id", "label", ...}], "edges": [{"label", "from", "to"}]}。id の形と property はモジュールの docstring の通り。
    辺の from / to は層の頂点か、物理層の interface（<機器>#<IF>）。無い頂点を指す辺は graph.seed_layers が飛ばして数える"""
    vertices, edges = [], []
    if_of = {d["device_id"]: {i["name"] for i in d.get("interfaces") or []} for d in devices}
    loopback_owner = {}   # ループバックのアドレス → 機器（BGP の相手を引く）
    for dev, c in cfg.items():
        for name, sub in (c.get("subinterfaces") or {}).items():
            if sub["interface"].startswith(LOOPBACK_PREFIXES) and sub.get("address"):
                loopback_owner[sub["address"]] = dev
    peer_of = {}   # (機器, IF) → (相手の機器, 相手の IF)
    for l in links:
        peer_of[(l["a"], l["a_if"])] = (l["b"], l["b_if"])
        peer_of[(l["b"], l["b_if"])] = (l["a"], l["a_if"])

    def add(vid, label, layer, **props):
        vertices.append({"id": vid, "label": label, "layer": layer, **props})

    def edge(label, src, dst):
        edges.append({"label": label, "from": src, "to": dst})

    for dev in sorted(cfg):
        c = cfg[dev]
        # IP 層: アドレスを持つサブインタフェース
        for name, sub in sorted((c.get("subinterfaces") or {}).items()):
            vid = f"{dev}#{name}"
            # ループバック（system0）は物理層に無いので interface_id を空にする（graph._props が落とす。over の辺も張らない）
            phys = f"{dev}#{sub['interface']}" if sub["interface"] in if_of.get(dev, set()) else ""
            add(vid, "ip_interface", "ip", device_id=dev, interface_id=phys, name=name,
                address=sub.get("address"), prefix_length=sub.get("prefix_length"), network_instance=sub.get("network_instance"))
            if phys:
                edge("over", vid, phys)
        # IP 層: IS-IS の隣接（passive でない IF。相手は物理の回線から）
        isis = c.get("isis") or {}
        for name, e in sorted((isis.get("interfaces") or {}).items()):
            if e.get("passive"):
                continue
            ifn = name.rsplit(".", 1)[0]
            peer_dev, peer_if = peer_of.get((dev, ifn), ("", ""))
            vid = f"{dev}#isis#{name}"
            add(vid, "isis_adjacency", "ip", device_id=dev, interface_id=f"{dev}#{ifn}", ip_interface_id=f"{dev}#{name}",
                instance=isis.get("instance"), peer_device=peer_dev)
            edge("over", vid, f"{dev}#{name}")
            if peer_dev and dev < peer_dev and (cfg.get(peer_dev, {}).get("isis") or {}).get("interfaces", {}).get(f"{peer_if}.0"):
                edge("peer", vid, f"{peer_dev}#isis#{peer_if}.0")
        # EVPN/BGP 層: BGP のセッション（ループバック同士）
        bgp = c.get("bgp") or {}
        lo_sub = next((n for n, sub in (c.get("subinterfaces") or {}).items() if sub["interface"].startswith(LOOPBACK_PREFIXES)), "")
        for ip, n in sorted((bgp.get("neighbors") or {}).items()):
            g = (bgp.get("groups") or {}).get(n.get("group") or "", {})
            local = g.get("local_address") or bgp.get("router_id") or ""
            peer_dev = loopback_owner.get(ip, "")
            vid = f"{dev}#bgp#{ip}"
            add(vid, "bgp_session", "evpn", device_id=dev, peer_address=ip, local_address=local, ip_interface_id=f"{dev}#{lo_sub}" if lo_sub else "",
                peer_device=peer_dev, group=n.get("group") or "", afi="evpn", asn=c.get("asn"), peer_as=g.get("peer_as"),
                role="route-reflector" if g.get("rr_client") else "client")
            if lo_sub:
                edge("over", vid, f"{dev}#{lo_sub}")
            if peer_dev and dev < peer_dev and local in (cfg.get(peer_dev, {}).get("bgp") or {}).get("neighbors", {}):
                edge("peer", vid, f"{peer_dev}#bgp#{local}")
        # EVPN/BGP 層: EVI（mac-vrf）と Ethernet Segment
        vtep = (c.get("subinterfaces") or {}).get(lo_sub, {}).get("address") if lo_sub else None
        for ni, e in sorted((c.get("evpn") or {}).items()):
            if e.get("evi") is None:
                continue
            vid = f"{dev}#evi#{e['evi']}"
            add(vid, "evpn_instance", "evpn", device_id=dev, network_instance=ni, evi=e["evi"], vni=e.get("vni"),
                route_target=e.get("route_target"), vtep=vtep, ip_interface_id=f"{dev}#{lo_sub}" if lo_sub else "",
                interfaces=",".join(e.get("interfaces") or []))
            if lo_sub:
                edge("over", vid, f"{dev}#{lo_sub}")
            for sub in e.get("interfaces") or []:
                edge("attach", vid, f"{dev}#{sub.rsplit('.', 1)[0]}")
        for name, e in sorted((c.get("es") or {}).items()):
            vid = f"{dev}#es#{name}"
            add(vid, "ethernet_segment", "evpn", device_id=dev, name=name, esi=e.get("esi"), mode=e.get("mode"),
                interface=e.get("interface"), interface_id=f"{dev}#{e['interface']}" if e.get("interface") else "")
            if e.get("interface"):
                edge("over", vid, f"{dev}#{e['interface']}")
    # 同じ EVI 同士（VXLAN のトンネル）と同じ ESI 同士（multihoming の組）
    by_evi, by_esi = {}, {}
    for v in vertices:
        if v["label"] == "evpn_instance":
            by_evi.setdefault(v["evi"], []).append(v["id"])
        elif v["label"] == "ethernet_segment" and v.get("esi"):
            by_esi.setdefault(v["esi"], []).append(v["id"])
    for group, label in ((by_evi, "tunnel"), (by_esi, "segment")):
        for ids in group.values():
            ids = sorted(ids)
            for i, x in enumerate(ids):
                for y in ids[i + 1:]:
                    edge(label, x, y)
    return {"vertices": vertices, "edges": edges}


def load(lab_dir: str) -> tuple[list[dict], list[dict], dict]:
    """(devices, links, layers)"""
    path = os.path.join(lab_dir, TOPO_FILE)
    with open(path, encoding="utf-8") as f:
        topo = load_yaml(f.read())
    cfg = {}
    cfg_dir = os.path.join(lab_dir, "srlinux")
    for fn in sorted(os.listdir(cfg_dir)) if os.path.isdir(cfg_dir) else []:
        if fn.endswith(".cli"):
            with open(os.path.join(cfg_dir, fn), encoding="utf-8") as f:
                cfg[fn[:-4]] = parse_srl(f.read())
    devices, links = build(topo, cfg)
    return devices, links, layers(devices, links, cfg)


FLAGS = {"--device-map", "--snmp-agents", "--gnmi-targets", "--layers"}

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if flags - FLAGS or len(flags) > 1:
        sys.exit("使い方: lab_topology.py [lab のディレクトリ] [--device-map | --snmp-agents | --gnmi-targets | --layers]")
    devices, links, lyr = load(args[0] if args else os.path.dirname(os.path.abspath(__file__)))
    if "--device-map" in flags:
        print(device_map(devices))
    elif "--snmp-agents" in flags or "--gnmi-targets" in flags:
        out = snmp_agents(devices) if "--snmp-agents" in flags else gnmi_targets(devices)
        if not out:
            sys.exit("監視対象（SNMP の設定を持つ機器）が 1 台も無い")
        print(out)
    elif "--layers" in flags:
        json.dump(lyr, sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        json.dump({"devices": devices, "links": links, "layers": lyr}, sys.stdout, ensure_ascii=False, indent=1)
        print()
