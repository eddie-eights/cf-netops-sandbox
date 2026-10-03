"""Spine-Leaf の lab（containerlab のトポロジ + Nokia SR Linux の設定）を作る。splab.clab.yml.in と srlinux/<機器>.cli の唯一の元。

構成（既定。すべて架空のアドレス）:
    wan-upstream-01（上流 VM。bond0 = LACP）
       │      │
    dc1-leafsw-01  dc1-leafsw-02      ← Leaf-SW（上流側）。EVPN multihoming（ES-1、all-active）
       │  ╲    ╱  │
    dc1-spine-01  dc1-spine-02        ← Spine（BGP EVPN のルートリフレクタ）
       │  ╱    ╲  │
    dc1-leaf-01   dc1-leaf-02         ← Leaf（アクセス側。実機なら 2 台）。EVPN multihoming（ES-2、all-active）
       │      │
    dc1-host-01（アクセス側 VM。bond0 = LACP）

  - 物理層: spine と leaf / leafsw は full mesh（spine 側 ethernet-1/<leaf の番号>、leaf 側 ethernet-1/<spine の番号>）。
    VM は leaf の組（01/02）に 2 本で dual-home し、leaf 側は LAG（lag1、LACP。組の 2 台が同じ LACP system-id を名乗る）
  - IP 層: fabric は /31（172.16.x.y）、ループバック system0 は 10.255.<段>.<番号>/32。IS-IS（instance main、L2、point-to-point）で配る
  - EVPN/BGP 層: iBGP AS 65100、EVPN の AFI だけ。spine がルートリフレクタで leaf / leafsw がクライアント（ループバック同士で張る）。
    mac-vrf macvrf-100（EVI 100、VNI 100、RT target:65100:100）に VM の LAG を入れ、VXLAN（vxlan1.100）で leaf と leafsw のあいだを通す。
    上流 VM とアクセス側 VM は同じ L2（10.100.0.0/24）に見える
  - 監視: 6 台とも SNMP の trap（linkDown / linkUp）と syslog を 203.0.113.1（lab の EC2）へ。gNMI（57400）は containerlab が有効にする

  SR-MPLS は SR Linux のコンテナでは ixr6e / ixr10e（ライセンスが要る）だけなので、いまは VXLAN。ライセンスが来たら `type:` を ixr6e にして
  トンネルを SR-MPLS に替える（docs/pipeline.md「lab」）。

台数を増やす（大規模化の練習）: python3 lab/gen_lab.py --leaves 4 --spines 3
  leaf は 2 台 1 組で、組ごとに VM（dc1-host-0N）が 1 台付く。leafsw と上流 VM はいつも 1 組。書き出した結果はそのまま
  lab_topology.py が読んで Neptune / Telegraf / Spark に配る（機器の一覧は lab の定義 1 か所）。既定の出力は git に入れてあり、
  tests/test_sync.py が「既定で作り直しても同じ」ことを確かめる（手で直すとテストが落ちる。直すならここを直して作り直す）。

使い方: python3 lab/gen_lab.py [--leaves N] [--spines N] [--out <lab のディレクトリ>]
"""

import argparse
import os

MGMT_SUBNET = "203.0.113.0/24"
MGMT_GW = "203.0.113.1"        # lab の EC2 側（trap / syslog の宛先）。lab.sh の MGMT_GW と同じ
LOG_PORT = 5140                # 機器の syslog の宛先ポート。lab.sh の LOG_PORT と telegraf.conf.in の inputs.syslog と同じ
LOG_FACILITY = "local7"        # 機器の syslog のファシリティ。本番の Cisco（IOS の既定）に合わせる
AS = 65100
EVI = 100
VNI = 100
L2_SUBNET = "10.100.0"         # VM が乗る L2（macvrf-100）。上流 VM が .10、アクセス側 VM が .20 から
PORT_SPEED = "25G"             # ixr-d2l の ethernet-1/1〜48 は 25G
BANDWIDTH = 25000


def mgmt_ip(n: int) -> str:
    return f"203.0.113.{n}"


class Switch:
    def __init__(self, name, tier, index, mgmt, loopback):
        self.name, self.tier, self.index, self.mgmt, self.loopback = name, tier, index, mgmt, loopback
        self.ifaces = []      # (ifname, description, address/len, peer)  fabric の /31
        self.lag = None       # (member if, pair id, host name)

    @property
    def net(self) -> str:
        return f"49.0001.0000.0000.{int(self.mgmt.rsplit('.', 1)[1]):04d}.00"


def plan(leaves: int, spines: int):
    """機器と回線を決める。戻り値は (switches, hosts, links)。links は [(a, a_if, b, b_if, comment)]（containerlab の endpoints）"""
    if leaves < 2 or leaves % 2 or spines < 1:
        raise SystemExit("--leaves は 2 以上の偶数（2 台 1 組）、--spines は 1 以上")
    leafsw = [Switch(f"dc1-leafsw-{i + 1:02d}", "leafsw", i, mgmt_ip(11 + i), f"10.255.1.{i + 1}") for i in range(2)]
    spine = [Switch(f"dc1-spine-{i + 1:02d}", "spine", i, mgmt_ip(21 + i), f"10.255.0.{i + 1}") for i in range(spines)]
    leaf = [Switch(f"dc1-leaf-{i + 1:02d}", "leaf", i, mgmt_ip(31 + i), f"10.255.2.{i + 1}") for i in range(leaves)]
    if spines > 8 or leaves > 60:
        raise SystemExit("spine は 8 台まで、leaf は 60 台まで（管理アドレスとポートの割り当ての都合）")
    downs = leafsw + leaf     # spine から見た下側（ポートの順）
    links = []
    for s in spine:
        for j, d in enumerate(downs):
            i = s.index * len(downs) + j
            s_addr, d_addr = f"172.16.{i // 128}.{(2 * i) % 256}", f"172.16.{i // 128}.{(2 * i + 1) % 256}"
            s.ifaces.append((f"ethernet-1/{j + 1}", f"fabric to {d.name} {PORT_SPEED}", f"{s_addr}/31", d.name))
            d.ifaces.append((f"ethernet-1/{s.index + 1}", f"fabric to {s.name} {PORT_SPEED}", f"{d_addr}/31", s.name))
            links.append((s.name, f"e1-{j + 1}", d.name, f"e1-{s.index + 1}", f"{s_addr}/31 - {d_addr}/31"))
    host_port = spines + 1
    hosts = []   # (name, mgmt, address, pair id, [(switch, port)])
    pairs = [(1, leafsw, "wan-upstream-01", mgmt_ip(101), f"{L2_SUBNET}.10")]
    for k in range(leaves // 2):
        pairs.append((2 + k, leaf[2 * k:2 * k + 2], f"dc1-host-{k + 1:02d}", mgmt_ip(102 + k), f"{L2_SUBNET}.{20 + k}"))
    for pid, pair, hname, hmgmt, haddr in pairs:
        ends = []
        for n, sw in enumerate(pair):
            sw.lag = (f"ethernet-1/{host_port}", pid, hname)
            ends.append((sw.name, f"e1-{host_port}"))
            links.append((hname, f"eth{n + 1}", sw.name, f"e1-{host_port}", f"{hname} bond0 (LACP) member {n + 1}"))
        hosts.append((hname, hmgmt, haddr, pid, ends))
    return leafsw + spine + leaf, hosts, links


# ---------------------------------------------------------------- SR Linux の設定（`set /` の行だけ）
def srl_config(sw: Switch, switches: list, hosts: list) -> str:
    lines = [f"set / system name host-name {sw.name}"]
    for ifn, desc, addr, _ in sw.ifaces:
        lines += [
            f'set / interface {ifn} description "{desc}"',
            f"set / interface {ifn} admin-state enable",
            f"set / interface {ifn} subinterface 0 admin-state enable",
            f"set / interface {ifn} subinterface 0 ipv4 admin-state enable",
            f"set / interface {ifn} subinterface 0 ipv4 address {addr}",
        ]
    if sw.lag:
        member, pid, hname = sw.lag
        lines += [
            f'set / interface {member} description "lag1 member to {hname} {PORT_SPEED}"',
            f"set / interface {member} admin-state enable",
            f"set / interface {member} ethernet aggregate-id lag1",
            f'set / interface lag1 description "LAG to {hname} (EVPN multihoming ES-{pid}, all-active)"',
            "set / interface lag1 admin-state enable",
            "set / interface lag1 subinterface 0 type bridged",
            "set / interface lag1 subinterface 0 admin-state enable",
            "set / interface lag1 lag lag-type lacp",
            f"set / interface lag1 lag member-speed {PORT_SPEED}",
            "set / interface lag1 lag lacp interval FAST",
            "set / interface lag1 lag lacp lacp-mode ACTIVE",
            f"set / interface lag1 lag lacp admin-key {10 + pid}",
            f"set / interface lag1 lag lacp system-id-mac 00:00:00:00:00:{pid:02x}",
            "set / interface lag1 lag lacp system-priority 11",
        ]
    lines += [
        "set / interface system0 admin-state enable",
        "set / interface system0 subinterface 0 admin-state enable",
        "set / interface system0 subinterface 0 ipv4 admin-state enable",
        f"set / interface system0 subinterface 0 ipv4 address {sw.loopback}/32",
    ]
    if sw.lag:
        member, pid, _ = sw.lag
        lines += [
            f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment ES-{pid} admin-state enable",
            f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment ES-{pid} esi 00:11:11:11:11:11:11:00:00:{pid:02x}",
            f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment ES-{pid} multi-homing-mode all-active",
            f"set / system network-instance protocols evpn ethernet-segments bgp-instance 1 ethernet-segment ES-{pid} interface lag1",
            "set / system network-instance protocols bgp-vpn bgp-instance 1",
            f"set / tunnel-interface vxlan1 vxlan-interface {VNI} type bridged",
            f"set / tunnel-interface vxlan1 vxlan-interface {VNI} ingress vni {VNI}",
        ]
    # underlay（default）: IS-IS と、EVPN の iBGP
    lines += ["set / network-instance default type default"]
    lines += [f"set / network-instance default interface {ifn}.0" for ifn, *_ in sw.ifaces]
    lines += [
        "set / network-instance default interface system0.0",
        "set / network-instance default protocols isis instance main admin-state enable",
        "set / network-instance default protocols isis instance main level-capability L2",
        f"set / network-instance default protocols isis instance main net [ {sw.net} ]",
        "set / network-instance default protocols isis instance main ipv4-unicast admin-state enable",
    ]
    for ifn, *_ in sw.ifaces:
        lines += [
            f"set / network-instance default protocols isis instance main interface {ifn}.0 circuit-type point-to-point",
            f"set / network-instance default protocols isis instance main interface {ifn}.0 ipv4-unicast admin-state enable",
        ]
    lines += [
        "set / network-instance default protocols isis instance main interface system0.0 passive true",
        "set / network-instance default protocols isis instance main interface system0.0 ipv4-unicast admin-state enable",
        f"set / network-instance default protocols bgp autonomous-system {AS}",
        f"set / network-instance default protocols bgp router-id {sw.loopback}",
        "set / network-instance default protocols bgp afi-safi evpn admin-state enable",
        "set / network-instance default protocols bgp afi-safi ipv4-unicast admin-state disable",
        f"set / network-instance default protocols bgp group overlay peer-as {AS}",
        "set / network-instance default protocols bgp group overlay afi-safi evpn admin-state enable",
        "set / network-instance default protocols bgp group overlay afi-safi ipv4-unicast admin-state disable",
        f"set / network-instance default protocols bgp group overlay transport local-address {sw.loopback}",
        "set / network-instance default protocols bgp group overlay timers keepalive-interval 10",
        "set / network-instance default protocols bgp group overlay timers hold-time 30",
    ]
    if sw.tier == "spine":
        lines += [
            "set / network-instance default protocols bgp group overlay route-reflector client true",
            f"set / network-instance default protocols bgp group overlay route-reflector cluster-id {sw.loopback}",
        ]
        peers = [s for s in switches if s.tier != "spine"]
    else:
        peers = [s for s in switches if s.tier == "spine"]
    for p in peers:
        lines += [
            f"set / network-instance default protocols bgp neighbor {p.loopback} peer-group overlay",
            f'set / network-instance default protocols bgp neighbor {p.loopback} description "{p.name}"',
        ]
    # overlay（mac-vrf）: VM の LAG を EVI 100 に入れ、VXLAN で他の leaf / leafsw と結ぶ
    if sw.lag:
        ni = f"macvrf-{EVI}"
        lines += [
            f"set / network-instance {ni} type mac-vrf",
            f"set / network-instance {ni} admin-state enable",
            f'set / network-instance {ni} description "EVI {EVI} (VNI {VNI}) upstream VM - access VM L2 {L2_SUBNET}.0/24"',
            f"set / network-instance {ni} interface lag1.0",
            f"set / network-instance {ni} vxlan-interface vxlan1.{VNI}",
            f"set / network-instance {ni} protocols bgp-evpn bgp-instance 1 admin-state enable",
            f"set / network-instance {ni} protocols bgp-evpn bgp-instance 1 vxlan-interface vxlan1.{VNI}",
            f"set / network-instance {ni} protocols bgp-evpn bgp-instance 1 evi {EVI}",
            f"set / network-instance {ni} protocols bgp-evpn bgp-instance 1 ecmp 2",
            f"set / network-instance {ni} protocols bgp-vpn bgp-instance 1 route-target export-rt target:{AS}:{EVI}",
            f"set / network-instance {ni} protocols bgp-vpn bgp-instance 1 route-target import-rt target:{AS}:{EVI}",
        ]
    # 監視: trap と syslog を lab の EC2（lab.sh forward が Telegraf へ DNAT）へ
    lines += [
        "set / system snmp network-instance mgmt admin-state enable",
        "set / system snmp trap-group telegraf admin-state enable",
        "set / system snmp trap-group telegraf network-instance mgmt",
        "set / system snmp trap-group telegraf destination telegraf admin-state enable",
        f"set / system snmp trap-group telegraf destination telegraf address {MGMT_GW}",
        "set / system snmp trap-group telegraf destination telegraf security-level no-auth-no-priv",
        "set / system snmp trap-group telegraf destination telegraf community-entry telegraf community public",  # 名前を community と同じにすると SR Linux が commit を拒む（2026-09-26 実測）
        "set / system logging network-instance mgmt",
        # 本番は Cisco（IOS の既定は local7）を想定するので、SR Linux の subsystem のログも local7 で出す（SR Linux の既定は local6）
        f"set / system logging subsystem-facility {LOG_FACILITY}",
        f"set / system logging remote-server {MGMT_GW} transport udp",
        f"set / system logging remote-server {MGMT_GW} remote-port {LOG_PORT}",
    ]
    for sub in ("bgp", "chassis", "evpn", "isis", "lag", "linux", "netinst", "xdp"):
        lines.append(f"set / system logging remote-server {MGMT_GW} subsystem {sub} priority match-above informational")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- containerlab のトポロジ（テンプレート）
def clab_template(switches: list, hosts: list, links: list, leaves: int, spines: int) -> str:
    tier_ja = {"leafsw": "Leaf-SW（上流側）", "spine": "Spine（EVPN のルートリフレクタ）", "leaf": "Leaf（アクセス側）"}
    out = [
        "# Spine-Leaf の DC ファブリック（IS-IS underlay + iBGP EVPN + VXLAN。すべて架空のアドレス）。lab/gen_lab.py が作る。**手で直さない**",
        f"# （python3 lab/gen_lab.py --leaves {leaves} --spines {spines} で作り直す。既定の出力は tests/test_sync.py が確かめる）。",
        "#",
        "# これはテンプレート。__SRLINUX_IMAGE__ などは lab.sh render が ECR の URI に置き換えて splab.clab.yml を作る。",
        "# スイッチは Nokia SR Linux（kind nokia_srlinux、type ixr-d2l = ライセンス不要。SR-MPLS にするときは ixr6e + ライセンス）。",
        "# 設定は srlinux/<機器名>.cli（`set /` の行だけ。containerlab が candidate に流して commit save する）。",
        "# SNMP（v2c、community public）と gNMI（57400、TLS）は containerlab が全ノードで有効にする。6 台とも trap（linkDown / linkUp）と",
        f"# syslog の宛先を {MGMT_GW}（この管理網の EC2 側）に向け、lab.sh forward がそれを Telegraf（ECS）の NLB へ DNAT する。",
        "#",
        "#   wan-upstream-01 ═══ dc1-leafsw-01 / dc1-leafsw-02   （LACP の bond0。leafsw 側は lag1 + EVPN multihoming ES-1）",
        "#                          ╲  ╳  ╱",
        "#                   dc1-spine-01 ... dc1-spine-0N        （full mesh。/31 + IS-IS）",
        "#                          ╱  ╳  ╲",
        "#   dc1-host-01     ═══ dc1-leaf-01 / dc1-leaf-02       （LACP の bond0。leaf 側は lag1 + EVPN multihoming ES-2）",
        "#",
        "# 機器名は <site>-<role>-<連番2桁>（role = leafsw / spine / leaf / upstream / host）。SR Linux の host-name（= SNMP の sysName、syslog の",
        "# hostname）も同じ値なので、collector は SNMP 側の sysName で機器を突き合わせられる。",
        "# インタフェースは containerlab の endpoints では e1-N、SR Linux の設定と ifTable（ifName）では ethernet-1/N。lab_topology.py が読み替える。",
        "name: splab",
        "",
        "mgmt:",
        "  # 名前と subnet を固定する（EC2 の中だけの docker network）。VPC からは Telegraf のタスクだけが、VPC のルートで lab の EC2 を経由して届く",
        "  # （terraform/pipeline/lab の locals.tf の mgmt_cidr と lab.sh の MGMT が同じ subnet）",
        "  network: nwc-lab",
        f"  ipv4-subnet: {MGMT_SUBNET}",
        "",
        "topology:",
        "  defaults:",
        "    env:",
        "      TZ: Asia/Tokyo",
        "",
        "  kinds:",
        "    nokia_srlinux:",
        "      image: __SRLINUX_IMAGE__",
        "      type: ixr-d2l",
        "    linux:",
        "      image: __MULTITOOL_IMAGE__",
        "",
        "  nodes:",
    ]
    for tier in ("leafsw", "spine", "leaf"):
        out.append(f"    # ---------- {tier_ja[tier]} ----------")
        for sw in [s for s in switches if s.tier == tier]:
            out += [
                f"    {sw.name}:",
                "      kind: nokia_srlinux",
                f"      group: {tier}",
                f"      mgmt-ipv4: {sw.mgmt}",
                f"      startup-config: srlinux/{sw.name}.cli",
                "",
            ]
    out.append("    # ---------- VM（linux。eth1 / eth2 を LACP の bond0 にまとめて leaf の組へ dual-home。EC2 側に bonding モジュールが要る）----------")
    for hname, hmgmt, haddr, pid, ends in hosts:
        out += [
            f"    {hname}:",
            "      kind: linux",
            f"      group: {'upstream' if pid == 1 else 'host'}",
            f"      mgmt-ipv4: {hmgmt}",
            "      exec:",
            "        - ip link add bond0 type bond mode 802.3ad miimon 100 lacp_rate fast",
        ]
        for n in range(len(ends)):
            out += [f"        - ip link set eth{n + 1} down", f"        - ip link set eth{n + 1} master bond0"]
        out += [
            "        - ip link set bond0 up",
            f"        - ip addr add {haddr}/24 dev bond0",
            "",
        ]
    out += ["  links:", "    # fabric（spine - leaf / leafsw の /31。SR Linux 側の e1-N は設定の ethernet-1/N）"]
    for a, a_if, b, b_if, comment in links:
        if a.startswith("dc1-spine-"):
            out.append(f'    - endpoints: ["{a}:{a_if}", "{b}:{b_if}"]   # {comment}')
    out.append("    # VM の LAG（各 VM から leaf の組へ 2 本）")
    for a, a_if, b, b_if, comment in links:
        if not a.startswith("dc1-spine-"):
            out.append(f'    - endpoints: ["{a}:{a_if}", "{b}:{b_if}"]   # {comment}')
    return "\n".join(out) + "\n"


def generate(leaves: int, spines: int) -> dict:
    """{相対パス: 中身}。splab.clab.yml.in と srlinux/<機器>.cli"""
    switches, hosts, links = plan(leaves, spines)
    files = {"splab.clab.yml.in": clab_template(switches, hosts, links, leaves, spines)}
    for sw in switches:
        files[f"srlinux/{sw.name}.cli"] = srl_config(sw, switches, hosts)
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--leaves", type=int, default=2, help="アクセス側の leaf の数（2 台 1 組。既定 2）")
    ap.add_argument("--spines", type=int, default=2, help="spine の数（既定 2）")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)), help="書き出す lab のディレクトリ（既定はこのファイルの場所）")
    a = ap.parse_args()
    files = generate(a.leaves, a.spines)
    srl_dir = os.path.join(a.out, "srlinux")
    os.makedirs(srl_dir, exist_ok=True)
    for fn in os.listdir(srl_dir):   # 前の台数の設定を残さない
        if fn.endswith(".cli") and f"srlinux/{fn}" not in files:
            os.remove(os.path.join(srl_dir, fn))
    for rel, text in files.items():
        with open(os.path.join(a.out, rel), "w", encoding="utf-8") as f:
            f.write(text)
    print(f"{a.out}: {len(files)} ファイル（SR Linux {len(files) - 1} 台）")


if __name__ == "__main__":
    main()
