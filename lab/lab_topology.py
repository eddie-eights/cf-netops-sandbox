"""lab の定義（containerlab のトポロジ + FRR の設定）から、Neptune に入れるトポロジ（機器と回線）を作る。

設計の「静的なトポロジ構成の取得・同期（初期 & 定期ロード）」の PoC 版。実機なら LLDP / BGP / NETCONF で取るところを、
lab には LLDP が無い（FRR）ので、機器の定義そのもの（wanlab.clab.yml.in の nodes / links と frr/<機器>.conf）から取る。
出す形は agent/data（devices.yaml + topology.json）と同じで、graph.seed() にそのまま渡せる。agent/data と同じ 10 台・10 本になることは
tests/test_sync.py が確かめる（lab を変えて agent/data を直し忘れるとテストが落ちる）。

読むもの:
  - nodes: 名前 <拠点>-<役割>-<連番>（拠点と役割は名前から。group は使わない）、mgmt-ipv4 → mgmt_ip、
    network-mode: container:<機器> の snmpd サイドカーが付く機器 → enabled（SNMP の監視対象）
  - links: endpoints ["x:ethN", "y:ethM"] → 回線（a < b に正規化）
  - frr/<機器>.conf: `router bgp <ASN>` → asn、`interface ethN` の description → 主/副（primary / secondary）と帯域（… 1G / 100M / 10G）、
    `ip address` → そのインタフェースのアドレス、`hostname` → 別名
  - nodes の exec の `ip addr add <アドレス>/<長さ> dev ethN`（host の LAN 側）→ そのインタフェースのアドレス
  - 回線の種別: 両端が pe なら ibgp、pe と ce なら ebgp、host が付くなら l2

機器ごとに、回線の端だけでなく機器が持つインタフェースを全部（interfaces。containerlab の管理 IF の eth0、FRR の interface、exec で
アドレスを振る IF。lo は除く）と、機器を指す別名（aliases。device_id / hostname / 管理 IP / 全インタフェースと lo のアドレス。小文字）を付ける。
検知（Spark）とトポロジ（Neptune）で機器とインタフェースの名前が合わずに異常がどこにも付かない、を減らすため。
機器の一覧はここ（lab の定義）1 か所にし、Telegraf のポーリング先と Spark の検知の device map もここから作る（ops/up.sh）。

使い方: python3 lab/lab_topology.py [lab のディレクトリ]  → JSON（{"devices": [...], "links": [...]}）を標準出力に出す。
        --device-map   Spark の検知の --device-map（別名=device_id,...。device_id と同じ別名は省く）を出す
        --snmp-agents  Telegraf の inputs.snmp の agents（監視対象の管理 IP。"udp://<IP>:161", ... の形）を出す
PyYAML があればそれで読み、無ければ（ops/up.sh を打つ PC の python3）この形の YAML だけ読める小さな読み取りで代える。
"""

import json
import os
import re
import sys

BANDWIDTH_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*([GM])\b")
EXEC_ADDR_RE = re.compile(r"^ip addr(?:ess)? add (\S+?)(?:/\d+)? dev (\S+)")
MGMT_IF = "eth0"      # containerlab が管理ネットワークにつなぐ IF（snmpd の ifTable にも出る）
SNMP_PORT = 161       # lab/snmpd/*.conf の agentaddress


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


# ---------------------------------------------------------------- FRR
def parse_frr(text: str) -> dict:
    """{"asn": int | None, "interfaces": {"eth1": {"description": "...", "address": "172.16.1.2"}}}（hostname があれば "hostname" も）"""
    asn, ifaces, cur, hostname = None, {}, None, None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^hostname (\S+)", line)
        if m:
            hostname = m.group(1)
            continue
        m = re.match(r"^interface (\S+)", line)
        if m:
            cur = ifaces.setdefault(m.group(1), {})
            continue
        m = re.match(r"^router bgp (\d+)", line)
        if m:
            asn, cur = int(m.group(1)), None
            continue
        if not line.startswith(" "):
            cur = None
            continue
        m = re.match(r"^ description (.+)$", line)
        if m and cur is not None:
            cur["description"] = m.group(1).strip()
            continue
        m = re.match(r"^ ip address (\S+?)(?:/\d+)?$", line)
        if m and cur is not None:
            cur.setdefault("address", m.group(1))
    out = {"asn": asn, "interfaces": ifaces}
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
    """<拠点>-<役割>-<連番> → (拠点, 役割)。carrier-pe-01 → (carrier, pe)"""
    parts = name.split("-")
    if len(parts) < 3:
        raise ValueError(f"機器名が <拠点>-<役割>-<連番> の形でない: {name}")
    return "-".join(parts[:-2]), parts[-2]


def _if_key(name: str):
    """eth2 < eth10 の順に並べる"""
    m = re.match(r"^(.*?)(\d+)$", name)
    return (m.group(1), int(m.group(2)), "") if m else (name, -1, name)


def _inventory(d: dict, spec: dict, frr: dict, link_ifs: set) -> None:
    """d に interfaces（[{"name", "address"}]）と aliases（小文字の別名）を足す"""
    addr = {}
    if d["mgmt_ip"]:
        addr[MGMT_IF] = d["mgmt_ip"]
    extra = []   # lo のアドレス（インタフェースには数えないが、trap の送り元になりうるので別名に入れる）
    for name, f in (frr.get("interfaces") or {}).items():
        if name.startswith("lo"):
            extra.append(f.get("address") or "")
        else:
            addr[name] = f.get("address") or addr.get(name, "")
    for cmd in spec.get("exec") or []:
        m = EXEC_ADDR_RE.match(str(cmd).strip())
        if m:
            addr[m.group(2)] = m.group(1)
    for name in link_ifs:
        addr.setdefault(name, "")
    d["interfaces"] = [{"name": n, "address": addr[n]} for n in sorted(addr, key=_if_key)]
    names = [d["device_id"], d["hostname"], frr.get("hostname") or "", d["mgmt_ip"], *addr.values(), *extra]
    d["aliases"] = sorted({str(x).strip().lower() for x in names if x})


def build(topo: dict, frr: dict) -> tuple[list[dict], list[dict]]:
    """topo は containerlab の YAML（辞書）、frr は {機器名: parse_frr の結果}。戻り値は (devices, links)"""
    nodes = topo["topology"]["nodes"]
    monitored = set()
    devices = []
    for name, spec in nodes.items():
        spec = spec or {}
        mode = str(spec.get("network-mode") or "")
        if mode.startswith("container:"):   # snmpd サイドカー。機器ではなく、相乗り先が監視対象という印
            monitored.add(mode.split(":", 1)[1])
            continue
        site, role = split_name(name)
        devices.append({"device_id": name, "hostname": name, "site": site, "role": role,
                        "mgmt_ip": spec.get("mgmt-ipv4") or "", "asn": frr.get(name, {}).get("asn")})
    for d in devices:
        d["enabled"] = d["device_id"] in monitored
    link_ifs = {}
    for item in topo["topology"].get("links") or []:
        for end in item["endpoints"]:
            dev, _, ifn = str(end).partition(":")
            link_ifs.setdefault(dev, set()).add(ifn)
    for d in devices:
        _inventory(d, nodes[d["device_id"]] or {}, frr.get(d["device_id"], {}), link_ifs.get(d["device_id"], set()))
    role_of = {d["device_id"]: d["role"] for d in devices}
    links = []
    for item in topo["topology"].get("links") or []:
        ends = item["endpoints"]
        (a, a_if), (b, b_if) = (str(ends[0]).split(":", 1), str(ends[1]).split(":", 1))
        if a > b:
            a, a_if, b, b_if = b, b_if, a, a_if
        for x in (a, b):
            if x not in role_of:
                raise ValueError(f"links の {x} が nodes に無い")
        roles = {role_of[a], role_of[b]}
        if "host" in roles:
            kind = "l2"
        elif roles == {"pe"}:
            kind = "ibgp"
        elif "pe" in roles:
            kind = "ebgp"
        else:
            kind = "l2"
        descs = [frr.get(a, {}).get("interfaces", {}).get(a_if, {}).get("description", ""),
                 frr.get(b, {}).get("interfaces", {}).get(b_if, {}).get("description", "")]
        role = None
        if kind == "ebgp":
            role = next((r for r in map(link_role, descs) if r), "primary")   # 主副の記述が無い WAN は主回線 1 本
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
    """Telegraf の inputs.snmp の agents の中身（監視対象 = snmpd のサイドカーが付く機器の管理 IP）"""
    return ", ".join(f'"udp://{d["mgmt_ip"]}:{SNMP_PORT}"' for d in devices if d.get("enabled") and d.get("mgmt_ip"))


def load(lab_dir: str) -> tuple[list[dict], list[dict]]:
    path = os.path.join(lab_dir, "wanlab.clab.yml.in")
    with open(path, encoding="utf-8") as f:
        topo = load_yaml(f.read())
    frr = {}
    frr_dir = os.path.join(lab_dir, "frr")
    for fn in sorted(os.listdir(frr_dir)) if os.path.isdir(frr_dir) else []:
        if fn.endswith(".conf"):
            with open(os.path.join(frr_dir, fn), encoding="utf-8") as f:
                frr[fn[:-5]] = parse_frr(f.read())
    return build(topo, frr)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if flags - {"--device-map", "--snmp-agents"} or len(flags) > 1:
        sys.exit("使い方: lab_topology.py [lab のディレクトリ] [--device-map | --snmp-agents]")
    devices, links = load(args[0] if args else os.path.dirname(os.path.abspath(__file__)))
    if "--device-map" in flags:
        print(device_map(devices))
    elif "--snmp-agents" in flags:
        agents = snmp_agents(devices)
        if not agents:
            sys.exit("監視対象（snmpd のサイドカーが付く機器）が 1 台も無い")
        print(agents)
    else:
        json.dump({"devices": devices, "links": links}, sys.stdout, ensure_ascii=False, indent=1)
        print()
