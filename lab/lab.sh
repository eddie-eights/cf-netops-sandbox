#!/usr/bin/env bash
# lab EC2（terraform/pipeline/lab）の上で containerlab を動かす。user_data が /usr/local/bin/lab に置くので、SSM セッションから `sudo lab check` で使う。
#   lab.sh render | pull | up | down | status | check | snmp <node> | logs [node] | fail-main | heal-main | failover | clab <args...>
#   lab.sh forward | forward-status     （stream: 別の EC2 の Telegraf へ SNMP / trap / FRR のログを通す。up が毎回呼ぶ。Telegraf 自体は telegraf/telegraf.sh）
# 手元の containerlab と違うのは 3 つ: containerlab を直接呼ぶ（root）、イメージは ECR から取る（pull）、
# wanlab.clab.yml はテンプレート（.in）からイメージ URI を埋めて作る（render）。
set -euo pipefail
# /usr/local/bin/lab（シンボリックリンク）から呼ばれても、テンプレートのある src/ で動く
SELF=$(readlink -f "$0")
cd "$(dirname "$SELF")"
LAB=wanlab
TOPO=wanlab.clab.yml
# FRR のログの置き場（機器ごとに 1 ディレクトリ。コンテナの /var/log/frr に bind する）。forward が書く rsyslog の設定（rsyslog-frr.conf.in）がここを読む。
# src/ の下に置かないのは、user_data の aws s3 sync --delete が起動のたびに消すから
LOG_DIR=/var/log/netops-lab
# containerlab の管理ネットワーク（wanlab.clab.yml.in の mgmt）と、その上のこの EC2 のアドレス（snmpd の trap の宛先）。
# Telegraf の EC2 は VPC のルートでここへ来る（terraform/pipeline/lab の telegraf.tf の local.mgmt_cidr）
MGMT=203.0.113.0/24
MGMT_GW=203.0.113.1
# FRR のログを Telegraf へ送る TCP のポート（telegraf/telegraf.conf.in の socket_listener と terraform/pipeline/lab の local.log_port と同じ）
LOG_PORT=5140
RSYSLOG_CONF=/etc/rsyslog.d/netops-lab-frr.conf
# forward が入れる iptables の規則の目印（入れ直す前にこれの付いた規則を全部消す）
FW_TAG=netops-lab-telegraf
# terraform/pipeline/lab の user_data が書く。REGISTRY / FRR_IMAGE / SNMPD_IMAGE / MULTITOOL_IMAGE / AWS_REGION / PARAM_PREFIX
ENV_FILE=$(ls /etc/*-lab.env 2>/dev/null | head -1 || true)
[ -n "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

clab() { containerlab "$@"; }
x() { docker exec "clab-$LAB-$1" "${@:2}"; }
vty() { x "$1" vtysh -c "$2"; }
# ifDescr と ifOperStatus を ifIndex で突き合わせて出す。
# ifIndex は veth のグローバル採番なので再 deploy のたびに変わる。突き合わせは ifDescr で行うこと
snmp_if() {
  local n=$1
  paste -d'\t' \
    <(x "$n" snmpwalk -v2c -c public -Oqn 127.0.0.1 1.3.6.1.2.1.2.2.1.2) \
    <(x "$n" snmpwalk -v2c -c public -Oqn 127.0.0.1 1.3.6.1.2.1.2.2.1.8) \
  | sed 's/^\.1\.3\.6\.1\.2\.1\.2\.2\.1\.2\.//; s/\.1\.3\.6\.1\.2\.1\.2\.2\.1\.8\.[0-9]* //' \
  | awk -F'\t' '{split($1,a," "); printf "  ifIndex %-4s %-10s oper=%s\n", a[1], a[2], $2}' \
  | grep -vE '(tunl0|gre0|gretap0|erspan0|ip_vti0|ip6_vti0|sit0|ip6tnl0|ip6gre0)'
}
unforward() {  # forward が入れた規則（目印 ${FW_TAG}）を全部消す
  local t rules r
  for t in raw filter nat; do
    rules=$(iptables -t "$t" -S 2>/dev/null | grep -- "--comment $FW_TAG" || true)
    [ -n "$rules" ] || continue
    while read -ra r; do iptables -t "$t" -D "${r[@]:1}"; done <<<"$rules"
  done
}

case "${1:-}" in
  render)
    : "${FRR_IMAGE:?}" "${SNMPD_IMAGE:?}" "${MULTITOOL_IMAGE:?}"
    sed -e "s#__FRR_IMAGE__#$FRR_IMAGE#" -e "s#__SNMPD_IMAGE__#$SNMPD_IMAGE#" -e "s#__MULTITOOL_IMAGE__#$MULTITOOL_IMAGE#" \
      -e "s#__LOG_DIR__#$LOG_DIR#" "$TOPO.in" > "$TOPO"
    # bind の元が無いと containerlab が deploy で落ちる。FRR はコンテナの中の frr ユーザーで書くので、誰でも書ける形（sticky）にする
    for f in frr/*.conf; do
      n=$(basename "$f" .conf)
      [ "$n" = vtysh ] || install -d -m 1777 "$LOG_DIR/$n"
    done
    echo "$TOPO を作った（イメージは ${REGISTRY:-?}）"
    ;;
  pull)
    # ECR の認証は 12 時間で切れるので、毎回ログインしてから取る（署名はインスタンスロール）
    : "${REGISTRY:?}" "${AWS_REGION:?}"
    aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
    for i in "$FRR_IMAGE" "$SNMPD_IMAGE" "$MULTITOOL_IMAGE"; do docker pull -q "$i"; done
    ;;
  up)
    [ -f "$TOPO" ] || "$SELF" render
    docker image inspect "$FRR_IMAGE" >/dev/null 2>&1 || "$SELF" pull
    clab deploy -t "$TOPO" --reconfigure
    # Docker は管理ネットワークを作るたびに自分の MASQUERADE を nat の先頭に入れるので、deploy のあとに毎回入れ直す。
    # 失敗してもトポロジは上がっている（Telegraf に届かないだけ。sudo lab forward-status で見る）
    "$SELF" forward || echo "forward に失敗した（トポロジは動いている）。sudo lab forward-status で見る" >&2
    ;;
  down)   clab destroy -t "$TOPO" --cleanup ;;
  status) clab inspect -t "$TOPO" ;;
  clab)   clab "${@:2}" ;;
  check)
    echo "== BGP (hq-ce-01) =="; vty hq-ce-01 "show bgp summary"
    echo "== BGP (carrier-pe-01) =="; vty carrier-pe-01 "show bgp summary"
    echo "== hq-ce-01 の経路 (10/8) =="; vty hq-ce-01 "show ip route 10.0.0.0/8 longer-prefixes"
    echo "== 拠点間 ping（各ホスト → 他拠点の LAN）=="
    for src in hq:10.1.0.10 dc:10.2.0.10 br1:10.3.0.10 br2:10.4.0.10; do
      s=${src%%:*}
      for dst in 10.1.0.10 10.2.0.10 10.3.0.10 10.4.0.10; do
        [ "${src##*:}" = "$dst" ] && continue
        if x "$s-host-01" ping -c1 -W1 "$dst" >/dev/null 2>&1; then r=ok; else r=NG; fi
        printf '  %-4s -> %-10s %s\n' "$s" "$dst" "$r"
      done
    done
    echo "== SNMP（hq-ce-01 の ifDescr + ifOperStatus。ifIndex は veth の採番なので飛ぶ）=="
    snmp_if hq-snmp-01
    ;;
  snmp) snmp_if "${2:-hq-snmp-01}" ;;
  logs) tail -n "${LINES:-20}" "$LOG_DIR"/${2:-*}/frr.log ;;
  fail-main)
    echo "本社の主回線 (hq-ce-01 eth1 / carrier-pe-01) を落とす"
    x hq-ce-01 ip link set eth1 down
    echo "  BGP のホールドタイマは 30 秒。切替の確認は 'lab failover' が待ってくれる"
    ;;
  heal-main) echo "本社の主回線 (hq-ce-01 eth1) を戻す"; x hq-ce-01 ip link set eth1 up ;;
  failover)
    echo "== 切替前: hq -> br2 の経路 =="; vty hq-ce-01 "show ip route 10.4.0.0/24" | grep via
    "$SELF" fail-main
    echo "== 副回線へ切り替わるのを待つ（最大 60 秒）=="
    for i in $(seq 12); do
      if vty hq-ce-01 "show ip route 10.4.0.0/24" 2>/dev/null | grep -q "172.16.2.1"; then
        echo "  切替 OK（$((i*5)) 秒以内）"; break
      fi
      sleep 5
    done
    echo "== 切替後: hq -> br2 の経路 =="; vty hq-ce-01 "show ip route 10.4.0.0/24" | grep via
    echo "== 切替後: hq -> 各拠点の疎通 =="
    for dst in 10.2.0.10 10.3.0.10 10.4.0.10; do
      if x hq-host-01 ping -c1 -W2 "$dst" >/dev/null 2>&1; then r=ok; else r=NG; fi
      printf "  hq -> %-10s %s\n" "$dst" "$r"
    done
    echo "== SNMP で主回線の断が見えるか（hq-ce-01）=="
    # snmpd の ifOperStatus は実際のリンク状態から数秒遅れる。down が見えるまで最大 10 秒待つ
    for i in $(seq 10); do
      w=$(snmp_if hq-snmp-01)
      grep -qE ' eth1 +oper=down' <<<"$w" && break
      sleep 1
    done
    echo "$w"
    if iptables -t nat -S PREROUTING 2>/dev/null | grep -q -- "--comment $FW_TAG"; then
      echo "== Telegraf（stream。別の EC2）=="
      echo "  ポーリング（10 秒周期）と snmpd の linkDown トラップ（5 秒周期の monitor）が MSK に流れ、analytics の Spark が異常を DynamoDB に書く（EventBridge にも出す）。"
      echo "  GUI の「異常一覧」か、エージェントに「今の異常は？」と聞くと hq-ce-01 eth1 の link_down が出る。戻すのは 'lab heal-main'"
    fi
    ;;
  forward)
    # 別の EC2 の Telegraf（terraform/pipeline/lab の create_telegraf）へ 3 つを通す。アドレスは SSM の $PARAM_PREFIX/telegraf-address。
    # 無ければ（Telegraf を作っていない）何もしない。何度打っても同じ規則になる（目印の付いた規則を消してから入れる）
    : "${AWS_REGION:?}" "${PARAM_PREFIX:?}"
    t=$(aws ssm get-parameter --region "$AWS_REGION" --name "$PARAM_PREFIX/telegraf-address" --query Parameter.Value --output text 2>/dev/null) || t=""
    unforward
    if [ -z "$t" ]; then
      rm -f "$RSYSLOG_CONF"
      echo "SSM $PARAM_PREFIX/telegraf-address が無い（Telegraf の EC2 を作っていない）ので、Telegraf への転送は張らない"
      exit 0
    fi
    c=(-m comment --comment "$FW_TAG")
    # ポーリング: Telegraf → CE の snmpd（161/udp）。VPC のルートでこの EC2 に来る。Docker は外から管理ネットワークへの転送を落とすので DOCKER-USER で先に通す
    iptables -I DOCKER-USER 1 -s "$t" -d "$MGMT" -p udp --dport 161 "${c[@]}" -j ACCEPT
    # Docker 28 以降は raw の PREROUTING でブリッジ以外から来たコンテナ宛てを落とす。その前で抜ける（古い Docker では何もしない規則になる）
    iptables -t raw -I PREROUTING 1 -s "$t" -d "$MGMT" -p udp --dport 161 "${c[@]}" -j ACCEPT
    # trap: snmpd の宛先（この EC2 の $MGMT_GW:162）を Telegraf へ向け直す
    iptables -t nat -I PREROUTING 1 -s "$MGMT" -d "$MGMT_GW" -p udp --dport 162 "${c[@]}" -j DNAT --to-destination "$t:162"
    iptables -I DOCKER-USER 1 -s "$MGMT" -d "$t" -p udp --dport 162 "${c[@]}" -j ACCEPT
    # 送り元（機器の管理 IP）を残す。Docker の MASQUERADE（-s $MGMT ! -o <bridge>）より前で抜ける。Spark とエージェントは送り元の IP で機器を引く
    iptables -t nat -I POSTROUTING 1 -s "$MGMT" -d "$t" "${c[@]}" -j RETURN
    # FRR のログ: rsyslog が $LOG_DIR/<機器名>/frr.log を読み、「機器名 行」にして Telegraf の $LOG_PORT/tcp へ送る
    if command -v rsyslogd >/dev/null; then
      sed -e "s#__LOG_DIR__#$LOG_DIR#g" -e "s#__TELEGRAF__#$t#" -e "s#__LOG_PORT__#$LOG_PORT#" rsyslog-frr.conf.in > "$RSYSLOG_CONF"
      systemctl enable -q rsyslog
      systemctl restart rsyslog
    else
      echo "rsyslog が入っていないので、FRR のログは Telegraf に届かない（dnf install -y rsyslog のあと sudo lab forward）" >&2
    fi
    echo "Telegraf（${t}）へ通した: SNMP 161/udp の転送、trap 162/udp の DNAT、FRR のログ $LOG_PORT/tcp"
    ;;
  forward-status)
    echo "== iptables（目印 ${FW_TAG}）=="
    for tb in raw filter nat; do iptables -t "$tb" -S 2>/dev/null | grep -- "--comment $FW_TAG" || true; done
    echo "== nat POSTROUTING（$FW_TAG の RETURN が Docker の MASQUERADE より上にあること）=="
    iptables -t nat -S POSTROUTING
    echo "== rsyslog（FRR のログ → Telegraf）=="
    systemctl is-active rsyslog || true
    if [ -f "$RSYSLOG_CONF" ]; then grep -o 'target="[^"]*" port="[^"]*"' "$RSYSLOG_CONF"; else echo "  $RSYSLOG_CONF が無い"; fi
    ;;
  *) sed -n '2,4p' "$SELF"; exit 1 ;;
esac
