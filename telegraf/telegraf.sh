#!/usr/bin/env bash
# Telegraf のコンテナ（terraform/pipeline/stream の ECS。telegraf/Dockerfile）の入口。イメージの /usr/local/bin/tg。
#   tg run    （既定。ECS が起こす）設定を作って Telegraf を起こす
#   tg test | gnmi   （ECS Exec から打つ。コマンドは ops/up.sh の最後に出る）ポーリング / gNMI の購読を 1 回だけ回して標準出力に出す
# 機器（lab の EC2 の中の containerlab）への経路は lab の EC2 側で `sudo lab forward-status` を見る。
set -euo pipefail
TEMPLATE=/etc/telegraf/telegraf.conf.in
# コンテナの / は書けるが、書くのは /tmp だけにする（作り直せば消える）
CONF=/tmp/telegraf.conf
export AWS_CONFIG_FILE=/tmp/aws_config
# 機器の syslog を受ける UDP のポート（telegraf.conf.in の inputs.syslog と lab/lab.sh の LOG_PORT と同じ）
LOG_PORT=5140
# trap を受ける UDP のポート。機器は 162 に送り、NLB が 1162 に向ける（非 root は 1024 未満で待てない）
TRAP_PORT=1162

render() {
  # ECS のタスク定義の環境変数（terraform/pipeline/stream の telegraf.tf）を埋めて $CONF を作る:
  #   KAFKA_BROKERS  MSK のブローカー（host:9098 をカンマで。IAM 認証の口）
  #   SNMP_AGENTS    ポーリング先（"udp://<IP>:161", ...）。ops/up.sh が lab の定義から作る（lab/lab_topology.py --snmp-agents）
  #   GNMI_TARGETS   gNMI の購読先（"<IP>:57400", ...）。同じく lab/lab_topology.py --gnmi-targets
  #   AWS_REGION
  : "${AWS_REGION:?}" "${KAFKA_BROKERS:?}"
  local agents="${SNMP_AGENTS:-}" gnmi="${GNMI_TARGETS:-}" q
  # 形が崩れていると Telegraf が起きないので、決まった形だけ通す
  if ! printf '%s' "$agents" | grep -Eq '^"udp://[0-9.]+:[0-9]+"(, *"udp://[0-9.]+:[0-9]+")*$'; then
    echo "SNMP_AGENTS が無いか形が違う（ops/up.sh が lab の定義から作って terraform/pipeline/stream の snmp_agents に渡す）: $agents" >&2; exit 1
  fi
  if ! printf '%s' "$gnmi" | grep -Eq '^"[0-9.]+:[0-9]+"(, *"[0-9.]+:[0-9]+")*$'; then
    echo "GNMI_TARGETS が無いか形が違う（ops/up.sh が lab の定義から作って terraform/pipeline/stream の gnmi_targets に渡す）: $gnmi" >&2; exit 1
  fi
  if ! printf '%s' "$KAFKA_BROKERS" | grep -Eq '^[A-Za-z0-9.-]+:[0-9]+(,[A-Za-z0-9.-]+:[0-9]+)*$'; then
    echo "KAFKA_BROKERS の形が違う: $KAFKA_BROKERS" >&2; exit 1
  fi
  q=$(printf '"%s"' "${KAFKA_BROKERS//,/\",\"}")
  # Telegraf の MSK IAM 認証は profile の指定が要る（telegraf.conf.in の注記）。鍵を書かない [default] なので、
  # SDK はタスクロール（ECS が入れる AWS_CONTAINER_CREDENTIALS_RELATIVE_URI）を使う
  printf '[default]\nregion = %s\n' "$AWS_REGION" > "$AWS_CONFIG_FILE"
  sed -e "s#__KAFKA_BROKERS__#$q#" -e "s#__AWS_REGION__#$AWS_REGION#" -e "s#__SNMP_AGENTS__#$agents#" -e "s#__GNMI_TARGETS__#$gnmi#" "$TEMPLATE" > "$CONF"
  echo "$CONF を作った（brokers: ${KAFKA_BROKERS} / agents: ${agents} / gnmi: ${gnmi} / trap: ${TRAP_PORT}/udp / syslog: ${LOG_PORT}/udp）"
}

case "${1:-run}" in
  run)
    render
    exec telegraf --config "$CONF"
    ;;
  test)
    # ポーリングだけ 1 回まわして標準出力に出す（MSK には送らない）。機器に届かないときは lab の EC2 の forward を疑う
    [ -f "$CONF" ] || render
    telegraf --config "$CONF" --test --input-filter snmp
    ;;
  gnmi)
    # gNMI の購読を 20 秒だけ回して標準出力に出す（MSK には送らない。on_change は最初に今の状態を全部送るので、BGP / IS-IS の一覧が見える）
    [ -f "$CONF" ] || render
    timeout 20 telegraf --config "$CONF" --test --input-filter gnmi --test-wait 15 || true
    ;;
  *) sed -n '2,4p' "$0"; exit 1 ;;
esac
