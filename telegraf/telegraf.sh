#!/usr/bin/env bash
# Telegraf のコンテナ（terraform/pipeline/stream の ECS。telegraf/Dockerfile）の入口。イメージの /usr/local/bin/tg。
#   tg run    （既定。ECS が起こす）設定を作って Telegraf を起こす
#   tg test | gnmi   （ECS Exec から telegraf-poll のタスクで打つ。コマンドは ops/up.sh の最後に出る）ポーリング（SNMP_POLL=1 のときだけ）/ gNMI の購読を 1 回だけ回して標準出力に出す
#   tg render       設定を作るだけ（中身を見る。tests/test_lab_debug.py も手元でこれを回す）
# stream の ECS は同じイメージを TELEGRAF_ROLE で 2 つのタスクに分けて動かす（受ける telegraf = dial_out、取りにいく telegraf-poll = dial_in）。
# デバッグ用の EC2（cloudformation/lab-debug.yaml）は SINK=stdout・TELEGRAF_ROLE の既定（all）の 1 つで両方やる（lab/lab.sh telegraf）。
# 機器（lab の EC2 の中の containerlab）への経路は lab の EC2 側で `sudo lab forward-status` を見る。
set -euo pipefail
TEMPLATE=${TELEGRAF_TEMPLATE:-/etc/telegraf/telegraf.conf.in}
# コンテナの / は書けるが、書くのは /tmp だけにする（作り直せば消える）
CONF=${TELEGRAF_CONF:-/tmp/telegraf.conf}
export AWS_CONFIG_FILE="${CONF%/*}/aws_config"
# 出力先。kafka = MSK（stream の ECS。既定）/ stdout = 標準出力（デバッグ用の EC2。MSK が無い）。telegraf.conf.in の「>>> sink <名前>」の区間
SINK=${SINK:-kafka}
SINKS="kafka stdout"
# 役割。telegraf.conf.in の「>>> role <名前>」の区間。all（既定）は両方を残し、dial_out / dial_in は相手の区間を消す:
#   dial_out  機器から送ってくるものを受ける（trap / syslog / MDT）。NLB の後ろの telegraf。機器の一覧は要らない
#   dial_in   こちらから取りにいく（gNMI の購読と SNMP のポーリング、lab の gNMI の変換）。telegraf-poll。機器の一覧（GNMI_TARGETS / SNMP_AGENTS）が要る
TELEGRAF_ROLE=${TELEGRAF_ROLE:-all}
ROLES="dial_out dial_in"
# 機器の syslog の形式。RFC3164 = BSD 形式（本番の Cisco IOS の既定。既定）/ RFC5424 = 新しい形式（lab の SR Linux。ops/up.sh と lab/lab.sh が渡す）。
# inputs.syslog の syslog_standard。形式が違うと best_effort でも項目がきれいに取れない
SYSLOG_STANDARD=${SYSLOG_STANDARD:-RFC3164}
SYSLOG_STANDARDS="RFC3164 RFC5424"
# SNMP のポーリング（inputs.snmp。10 秒ごとに ifTable）。0 = 止める（既定。SNMP は trap だけ受ける）/ 1 = する。
# telegraf.conf.in の「>>> snmp_poll」の区間。stream の snmp_poll（ops/up.sh は deploy.env の SNMP_POLL）が渡す
SNMP_POLL=${SNMP_POLL:-0}
# 機器の syslog を受ける UDP のポート（telegraf.conf.in の inputs.syslog と lab/lab.sh の LOG_PORT と同じ）
LOG_PORT=5140
# trap を受ける UDP のポート。機器は 162 に送り、NLB が 1162 に向ける（非 root は 1024 未満で待てない）
TRAP_PORT=1162
# MDT の dial-out を受ける TCP のポート（telegraf.conf.in の inputs.cisco_telemetry_mdt と terraform/pipeline/stream の telegraf.tf）
MDT_PORT=57000

render() {
  # ECS のタスク定義の環境変数（terraform/pipeline/stream の telegraf.tf）を埋めて $CONF を作る:
  #   KAFKA_BROKERS  MSK のブローカー（host:9098 をカンマで。IAM 認証の口）。SINK=stdout では要らない
  #   TELEGRAF_ROLE  役割（all / dial_out / dial_in。既定 all）
  #   SNMP_POLL      SNMP のポーリングをするか（0 / 1、既定 0 = trap だけ）。stream の snmp_poll
  #   SNMP_AGENTS    ポーリング先（"udp://<IP>:161", ...）。ops/up.sh が lab の定義から作る（lab/lab_topology.py --snmp-agents）。SNMP_POLL=1 のときだけ見る
  #   GNMI_TARGETS   gNMI の購読先（"<IP>:57400", ...）。同じく lab/lab_topology.py --gnmi-targets
  #                  SNMP_AGENTS と GNMI_TARGETS は取りにいく入力のものなので、TELEGRAF_ROLE=dial_out では見ない
  #   SYSLOG_STANDARD  機器の syslog の形式（既定 RFC3164）。stream の syslog_standard。lab の SR Linux は RFC5424
  #   AWS_REGION
  : "${AWS_REGION:?}"
  local agents="${SNMP_AGENTS:-}" gnmi="${GNMI_TARGETS:-}" q="" s drop=() ins=""
  case " all $ROLES " in *" $TELEGRAF_ROLE "*) ;; *) echo "TELEGRAF_ROLE は all $ROLES のどれか: $TELEGRAF_ROLE" >&2; exit 1 ;; esac
  case " $SINKS " in *" $SINK "*) ;; *) echo "SINK は $SINKS のどれか: $SINK" >&2; exit 1 ;; esac
  case " $SYSLOG_STANDARDS " in *" $SYSLOG_STANDARD "*) ;; *) echo "SYSLOG_STANDARD は $SYSLOG_STANDARDS のどれか: $SYSLOG_STANDARD" >&2; exit 1 ;; esac
  case "$SNMP_POLL" in 0|1) ;; *) echo "SNMP_POLL は 0 か 1: $SNMP_POLL" >&2; exit 1 ;; esac
  # 選ばなかった役割の区間を消す（snmp_poll の区間は dial_in の中にあるので一緒に消える）
  for s in $ROLES; do [ "$TELEGRAF_ROLE" = all ] || [ "$s" = "$TELEGRAF_ROLE" ] || drop+=(-e "/^# >>> role $s/,/^# <<< role $s/d"); done
  if [ "$TELEGRAF_ROLE" = dial_out ]; then
    # 取りにいく入力は区間ごと消える（__SNMP_AGENTS__ / __GNMI_TARGETS__ もその中）ので、機器の一覧は見ない
    agents="" gnmi=""
  else
    # 形が崩れていると Telegraf が起きないので、決まった形だけ通す
    if [ "$SNMP_POLL" = 1 ]; then
      if ! printf '%s' "$agents" | grep -Eq '^"udp://[0-9.]+:[0-9]+"(, *"udp://[0-9.]+:[0-9]+")*$'; then
        echo "SNMP_AGENTS が無いか形が違う（ops/up.sh が lab の定義から作って terraform/pipeline/stream の snmp_agents に渡す）: $agents" >&2; exit 1
      fi
    else
      # ポーリングの区間ごと消す（__SNMP_AGENTS__ もその中なので、SNMP_AGENTS は見ない）
      drop+=(-e "/^# >>> snmp_poll/,/^# <<< snmp_poll/d")
      agents=""
    fi
    if ! printf '%s' "$gnmi" | grep -Eq '^"[0-9.]+:[0-9]+"(, *"[0-9.]+:[0-9]+")*$'; then
      echo "GNMI_TARGETS が無いか形が違う（ops/up.sh が lab の定義から作って terraform/pipeline/stream の gnmi_targets に渡す）: $gnmi" >&2; exit 1
    fi
    ins=" / snmp poll: ${agents:-off} / gnmi: ${gnmi}"
  fi
  [ "$TELEGRAF_ROLE" = dial_in ] || ins="$ins / trap: ${TRAP_PORT}/udp / syslog: ${LOG_PORT}/udp ${SYSLOG_STANDARD} / mdt: ${MDT_PORT}/tcp"
  if [ "$SINK" = kafka ]; then
    : "${KAFKA_BROKERS:?}"
    if ! printf '%s' "$KAFKA_BROKERS" | grep -Eq '^[A-Za-z0-9.-]+:[0-9]+(,[A-Za-z0-9.-]+:[0-9]+)*$'; then
      echo "KAFKA_BROKERS の形が違う: $KAFKA_BROKERS" >&2; exit 1
    fi
    q=$(printf '"%s"' "${KAFKA_BROKERS//,/\",\"}")
    # Telegraf の MSK IAM 認証は profile の指定が要る（telegraf.conf.in の注記）。鍵を書かない [default] なので、
    # SDK はタスクロール（ECS が入れる AWS_CONTAINER_CREDENTIALS_RELATIVE_URI）を使う
    printf '[default]\nregion = %s\n' "$AWS_REGION" > "$AWS_CONFIG_FILE"
  fi
  # 選ばなかった出力の区間を消す
  for s in $SINKS; do [ "$s" = "$SINK" ] || drop+=(-e "/^# >>> sink $s/,/^# <<< sink $s/d"); done
  sed "${drop[@]}" -e "s#__KAFKA_BROKERS__#$q#" -e "s#__AWS_REGION__#$AWS_REGION#" -e "s#__SNMP_AGENTS__#$agents#" -e "s#__GNMI_TARGETS__#$gnmi#" -e "s#__SYSLOG_STANDARD__#$SYSLOG_STANDARD#" "$TEMPLATE" > "$CONF"
  echo "$CONF を作った（role: ${TELEGRAF_ROLE} / sink: ${SINK}${q:+ / brokers: $KAFKA_BROKERS}${ins}）"
}

# tg test / gnmi が回す入力が描いた設定に無ければ、telegraf を呼ぶ前に分かる言葉で止まる
need_input() {
  grep -q "^\[\[inputs\.$1\]\]" "$CONF" && return 0
  if [ "$TELEGRAF_ROLE" = dial_out ]; then
    echo "このタスクは受けるだけ（TELEGRAF_ROLE=dial_out）。$1 は取りにいく側のタスク（stream の ECS の telegraf-poll。ops/up.sh の最後に出るコマンド）で打つ" >&2; exit 1
  fi
  echo "$2" >&2; exit 1
}

case "${1:-run}" in
  run)
    render
    exec telegraf --config "$CONF"
    ;;
  render) render ;;
  test)
    # ポーリングだけ 1 回まわして標準出力に出す（MSK には送らない）。機器に届かないときは lab の EC2 の forward を疑う
    [ -f "$CONF" ] || render
    need_input snmp "SNMP のポーリングは止めてある（SNMP_POLL=0。既定。SNMP は trap だけ受ける）。ポーリングするなら SNMP_POLL=1 で起こし直す（ops/up.sh なら deploy.env の SNMP_POLL=1）"
    telegraf --config "$CONF" --test --input-filter snmp
    ;;
  gnmi)
    # gNMI の購読を 20 秒だけ回して標準出力に出す（MSK には送らない。on_change は最初に今の状態を全部送るので、BGP / IS-IS の一覧が見える）
    [ -f "$CONF" ] || render
    need_input gnmi "gNMI の購読が設定に無い（$CONF）"
    timeout 20 telegraf --config "$CONF" --test --input-filter gnmi --test-wait 15 || true
    ;;
  *) sed -n '2,5p' "$0"; exit 1 ;;
esac
