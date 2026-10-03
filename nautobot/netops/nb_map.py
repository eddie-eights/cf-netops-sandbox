"""Nautobot の中身と、このリポジトリのトポロジの形（graph.seed() / graph.sync_physical() が受ける devices / links）の対応付け。

nautobot も boto3 も import しない純粋な関数だけを置く（手元の tests/test_nautobot.py で検査できるように。ORM を読むのは nb_sync.py）。

対応:
  機器          Device.name = device_id（hostname も同じ）、Location.name = site、Role.name = role（leaf / leafsw / spine / host / upstream）、
                primary_ip4 = mgmt_ip、custom field asn = asn
  監視対象      その Device に Service があること。gnmi（tcp）があれば gNMI を取りにいき、snmp（udp）があれば SNMP を取りにいく。
                どちらかがあれば enabled。Telegraf の dialin の一覧（lab/lab_topology.py の --gnmi-targets / --snmp-agents と同じ形）もここから作る
  インタフェース Interface.name、address = 最初の IP（長さ無し）、lag = LAG の親の name
  回線          Cable（両端が Interface）。a < b にそろえ、kind は lab/lab_topology.py と同じ規則で両端から決める。
                role / bandwidth_mbps は Cable の custom field link_role / bandwidth_mbps
"""
import ipaddress

VM_ROLES = {"host", "upstream"}   # スイッチでない機器の役割（lab/lab_topology.py と同じ）
GNMI_SERVICE = ("gnmi", "tcp", 57400)   # Service の name / protocol と、seed で入れるポート（lab/lab_topology.py の GNMI_PORT）
SNMP_SERVICE = ("snmp", "udp", 161)     # 同じく SNMP_PORT
TARGET_KEYS = ("gnmi-targets", "snmp-agents")   # terraform/pipeline/stream の出力 telegraf_dialin_target_parameters のキー


def _port(row: dict, service: tuple):
    """row の機器が service（name, protocol, _）を持っていれば最初のポート。無ければ None"""
    for s in row.get("services") or []:
        if str(s.get("name", "")).lower() == service[0] and s.get("protocol") == service[1] and s.get("ports"):
            return int(s["ports"][0])
    return None


def link_kind(role_a: str, role_b: str, lag_a: str, lag_b: str) -> str:
    """回線の種類。VM の回線はどちらかの端が LAG のメンバーなら lag、そうでなければ l2。スイッチどうしは spine が入れば fabric、それ以外は l2"""
    roles = {role_a, role_b}
    if roles & VM_ROLES:
        return "lag" if (lag_a or lag_b) else "l2"
    return "fabric" if "spine" in roles else "l2"


def to_graph(rows: list[dict], cables: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """(devices, links, warnings)。rows は機器（{name, site, role, mgmt_ip, asn, interfaces: [{name, address, lag}], services: [{name, protocol, ports}]}）、
    cables は回線（{a, a_if, b, b_if, role, bandwidth_mbps}）。名前の無い機器と、端の機器が rows に無い / 両端が同じ機器 / 重なった回線は落として warnings に書く"""
    warnings, devices = [], {}
    for r in rows:
        name = r.get("name")
        if not name:
            warnings.append("名前の無い機器を飛ばした")
            continue
        if name in devices:
            warnings.append(f"{name} が 2 台ある。後のほうを飛ばした")
            continue
        devices[name] = {
            "device_id": name, "hostname": name, "site": r.get("site") or "", "role": r.get("role") or "",
            "mgmt_ip": r.get("mgmt_ip") or "", "asn": int(r["asn"]) if r.get("asn") not in (None, "") else None,
            "enabled": _port(r, GNMI_SERVICE) is not None or _port(r, SNMP_SERVICE) is not None,
            "interfaces": sorted(({"name": i["name"], "address": i.get("address") or "", "lag": i.get("lag") or ""}
                                  for i in r.get("interfaces") or [] if i.get("name")), key=lambda i: i["name"]),
        }
    lag_of = {n: {i["name"]: i["lag"] for i in d["interfaces"]} for n, d in devices.items()}
    links = {}
    for c in cables:
        a, a_if, b, b_if = c.get("a"), c.get("a_if"), c.get("b"), c.get("b_if")
        if a not in devices or b not in devices or not a_if or not b_if:
            warnings.append(f"回線 {a}:{a_if} - {b}:{b_if} は端の機器かインタフェースが無いので飛ばした")
            continue
        if a == b:
            warnings.append(f"回線 {a}:{a_if} - {b}:{b_if} は両端が同じ機器なので飛ばした")
            continue
        if a > b:
            a, a_if, b, b_if = b, b_if, a, a_if
        if (a, b, a_if) in links:
            warnings.append(f"回線 {a}:{a_if} - {b} が重なっている。後のほうを飛ばした")
            continue
        links[(a, b, a_if)] = {
            "a": a, "a_if": a_if, "b": b, "b_if": b_if,
            "kind": link_kind(devices[a]["role"], devices[b]["role"], lag_of[a].get(a_if, ""), lag_of[b].get(b_if, "")),
            "role": c.get("role") or None,
            "bandwidth_mbps": int(c["bandwidth_mbps"]) if c.get("bandwidth_mbps") else None,
        }
    return [devices[n] for n in sorted(devices)], [links[k] for k in sorted(links)], warnings


def targets(rows: list[dict]) -> dict:
    """Telegraf の dialin の一覧（TOML のリストの中身）。{"gnmi-targets": '"<IP>:<port>", ...', "snmp-agents": '"udp://<IP>:<port>", ...'}。
    機器の名前の順（lab/lab_topology.py の gnmi_targets() / snmp_agents() と同じ並び）。管理 IP の無い機器は入らない"""
    out = {k: [] for k in TARGET_KEYS}
    for r in sorted((r for r in rows if r.get("name") and r.get("mgmt_ip")), key=lambda r: r["name"]):
        ip = str(ipaddress.ip_address(r["mgmt_ip"]))   # 一覧は telegraf.conf にそのまま入るので、IP でないものは通さない
        gnmi, snmp = _port(r, GNMI_SERVICE), _port(r, SNMP_SERVICE)
        if gnmi is not None:
            out["gnmi-targets"].append(f'"{ip}:{gnmi}"')
        if snmp is not None:
            out["snmp-agents"].append(f'"udp://{ip}:{snmp}"')
    return {k: ", ".join(v) for k, v in out.items()}


# ---------------------------------------------------------------- 最初の seed（lab の定義 → Nautobot）
def interface_type(name: str, lag_parents: set) -> str:
    """Nautobot の Interface.type（InterfaceTypeChoices の値）。LAG の親は lag、SR Linux の ethernet-* は 25G、それ以外（mgmt0 / eth*）は 1G"""
    if name in lag_parents:
        return "lag"
    return "25gbase-x-sfp28" if name.startswith("ethernet-") else "1000base-t"


def parent_prefix(address: str) -> str:
    """address を入れる親の Prefix（Nautobot の IPAddress は親の Prefix が要る）。v4 は /24、v6 は /64"""
    ip = ipaddress.ip_address(address)
    return str(ipaddress.ip_network(f"{ip}/{24 if ip.version == 4 else 64}", strict=False))


def seed_plan(lab: dict) -> dict:
    """lab/lab_topology.py の出力（{"devices", "links"}）から、Nautobot に作るものの一覧。
    {"sites", "roles", "prefixes", "devices": [{name, site, role, vm, asn, mgmt_ip, services: [(name, protocol, port)],
     interfaces: [{name, type, lag, address, mgmt}]}], "cables": [{a, a_if, b, b_if, role, bandwidth_mbps}]}。
    interfaces は LAG の親が先（メンバーが親を指せるように）。to_graph() に戻すと lab と同じ devices / links になる"""
    devices, prefixes = [], set()
    for d in lab["devices"]:
        ifs = d.get("interfaces") or []
        parents = {i["lag"] for i in ifs if i.get("lag")}
        rows = []
        for i in sorted(ifs, key=lambda i: (i["name"] not in parents, i["name"])):
            if i.get("address"):
                prefixes.add(parent_prefix(i["address"]))
            rows.append({"name": i["name"], "type": interface_type(i["name"], parents), "lag": i.get("lag") or "",
                         "address": i.get("address") or "", "mgmt": bool(d.get("mgmt_ip")) and i.get("address") == d.get("mgmt_ip")})
        devices.append({
            "name": d["device_id"], "site": d["site"], "role": d["role"], "vm": d["role"] in VM_ROLES, "asn": d.get("asn"),
            "mgmt_ip": d.get("mgmt_ip") or "", "interfaces": rows,
            "services": [(s[0], s[1], s[2]) for s in (GNMI_SERVICE, SNMP_SERVICE)] if d.get("enabled") else [],
        })
    return {
        "sites": sorted({d["site"] for d in devices}), "roles": sorted({d["role"] for d in devices}), "prefixes": sorted(prefixes),
        "devices": devices,
        "cables": [{k: l.get(k) for k in ("a", "a_if", "b", "b_if", "role", "bandwidth_mbps")} for l in lab["links"]],
    }
