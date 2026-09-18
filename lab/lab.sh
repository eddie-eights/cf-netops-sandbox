#!/usr/bin/env bash
# lab EC2（terraform/pipeline/lab）の上で containerlab を動かす。user_data が /usr/local/bin/lab に置くので、SSM セッションから `sudo lab check` で使う。
#   lab.sh render | pull | up | down | status | check | snmp <node> | logs [node] | fail-main | heal-main | failover | clab <args...>
#   lab.sh telegraf-render | telegraf-status     （stream: Telegraf → MSK。deploy.env の WITH_STREAM=1 で terraform/pipeline/stream を作ってから）
# 手元の containerlab と違うのは 3 つ: containerlab を直接呼ぶ（root）、イメージは ECR から取る（pull）、
# wanlab.clab.yml はテンプレート（.in）からイメージ URI を埋めて作る（render）。
set -euo pipefail
cd "$(dirname "$0")"
SELF="$PWD/$(basename "$0")"
LAB=wanlab
TOPO=wanlab.clab.yml
# FRR のログの置き場（機器ごとに 1 ディレクトリ。コンテナの /var/log/frr に bind する）。telegraf.conf.in の inputs.tail と同じパス。
# src/ の下に置かないのは、user_data の aws s3 sync --delete が起動のたびに消すから
LOG_DIR=/var/log/netops-lab
# terraform/pipeline/lab の user_data が書く。REGISTRY / FRR_IMAGE / SNMPD_IMAGE / MULTITOOL_IMAGE / AWS_REGION
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
    if systemctl is-active -q "*-telegraf.service" 2>/dev/null; then
      echo "== Telegraf（stream）=="
      echo "  ポーリング（10 秒周期）と snmpd の linkDown トラップ（5 秒周期の monitor）が MSK に流れ、analytics の Spark が異常を DynamoDB に書く（EventBridge にも出す）。"
      echo "  GUI の「異常一覧」か、エージェントに「今の異常は？」と聞くと hq-ce-01 eth1 の link_down が出る。戻すのは 'lab heal-main'"
    fi
    ;;
  telegraf-render)
    # terraform/pipeline/stream が SSM に書いたブローカーを埋めて /etc/telegraf/telegraf.conf を作る。
    # terraform/pipeline/stream が無いときは失敗して終わる（unit は Restart=on-failure で 60 秒ごとに試し直す）
    : "${AWS_REGION:?}" "${PARAM_PREFIX:?}"
    b=$(aws ssm get-parameter --region "$AWS_REGION" --name "$PARAM_PREFIX/msk-bootstrap" --query Parameter.Value --output text) || {
      echo "SSM $PARAM_PREFIX/msk-bootstrap が読めない。terraform/pipeline/stream はまだ？" >&2; exit 1; }
    q=$(printf '"%s"' "${b//,/\",\"}")
    install -d -m 0755 /etc/telegraf
    # Telegraf の MSK IAM 認証は profile の指定が要る（telegraf.conf.in の注記）。鍵を書かない [default] なので EC2 のロールが使われる
    printf '[default]\nregion = %s\n' "$AWS_REGION" > /etc/telegraf/aws_config
    sed -e "s#__KAFKA_BROKERS__#$q#" -e "s#__AWS_REGION__#$AWS_REGION#" telegraf.conf.in > /etc/telegraf/telegraf.conf
    echo "/etc/telegraf/telegraf.conf を作った（brokers: $b）"
    ;;
  telegraf-status)
    systemctl --no-pager status "*-telegraf.service" || true
    echo "== 直近のログ =="; journalctl -u "*-telegraf.service" -n 20 --no-pager
    ;;
  *) sed -n '2,4p' "$SELF"; exit 1 ;;
esac
