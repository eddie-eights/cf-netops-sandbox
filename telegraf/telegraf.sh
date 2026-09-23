#!/usr/bin/env bash
# Telegraf の EC2（terraform/pipeline/lab の create_telegraf）の上で Telegraf を扱う。user_data が /usr/local/bin/tg に置くので、
# SSM セッションから `sudo tg status` で使う。unit は /etc/systemd/system/<接頭辞>-telegraf.service（ExecStartPre が render）。
#   tg render | status | test | logs | restart
# 機器（lab の EC2 の中の containerlab）への経路は lab の EC2 側で `sudo lab forward-status` を見る。
set -euo pipefail
# /usr/local/bin/tg（シンボリックリンク）から呼ばれても、テンプレートのある src/ で動く
SELF=$(readlink -f "$0")
cd "$(dirname "$SELF")"
CONF=/etc/telegraf/telegraf.conf
# FRR のログを受ける TCP のポート（telegraf.conf.in の socket_listener と lab/lab.sh の LOG_PORT と同じ）
LOG_PORT=5140
# ポーリング先（"udp://<IP>:161", ...）。ops/up.sh が lab の定義から作って s3://<バケット>/telegraf/ に置き、user_data の s3 sync で src/ に来る
AGENTS_FILE=snmp_agents.txt
# terraform/pipeline/lab の user_data が書く。AWS_REGION / PARAM_PREFIX
ENV_FILE=$(ls /etc/*-telegraf.env 2>/dev/null | head -1 || true)
[ -n "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
UNIT=$(basename "${ENV_FILE:-/etc/telegraf.env}" .env).service

case "${1:-}" in
  render)
    # terraform/pipeline/stream が SSM に書いたブローカーと、ポーリング先（snmp_agents.txt）を埋めて /etc/telegraf/telegraf.conf を作る。
    # terraform/pipeline/stream が無いときは失敗して終わる（unit は Restart=on-failure で 60 秒ごとに試し直す）
    : "${AWS_REGION:?}" "${PARAM_PREFIX:?}"
    agents=""
    [ -f "$AGENTS_FILE" ] && agents=$(tr -d '\n' < "$AGENTS_FILE")
    # 形が崩れていると Telegraf が起きないので、"udp://<IPv4>:<ポート>" をカンマで並べた形だけ通す
    if ! printf '%s' "$agents" | grep -Eq '^"udp://[0-9.]+:[0-9]+"(, *"udp://[0-9.]+:[0-9]+")*$'; then
      echo "$PWD/$AGENTS_FILE が無いか形が違う（ops/up.sh の 5-1b が lab の定義から作って s3://<バケット>/telegraf/ に置く。置いたら EC2 を再起動）" >&2; exit 1
    fi
    b=$(aws ssm get-parameter --region "$AWS_REGION" --name "$PARAM_PREFIX/msk-bootstrap" --query Parameter.Value --output text) || {
      echo "SSM $PARAM_PREFIX/msk-bootstrap が読めない。terraform/pipeline/stream はまだ？" >&2; exit 1; }
    q=$(printf '"%s"' "${b//,/\",\"}")
    install -d -m 0755 /etc/telegraf
    # Telegraf の MSK IAM 認証は profile の指定が要る（telegraf.conf.in の注記）。鍵を書かない [default] なので EC2 のロールが使われる
    printf '[default]\nregion = %s\n' "$AWS_REGION" > /etc/telegraf/aws_config
    sed -e "s#__KAFKA_BROKERS__#$q#" -e "s#__AWS_REGION__#$AWS_REGION#" -e "s#__SNMP_AGENTS__#$agents#" telegraf.conf.in > "$CONF"
    echo "$CONF を作った（brokers: ${b} / agents: ${agents}）"
    ;;
  status)
    systemctl --no-pager status "$UNIT" || true
    echo "== 受けているポート（trap 162/udp、FRR のログ $LOG_PORT/tcp）=="
    ss -lunp 'sport = :162' || true
    ss -ltnp "sport = :$LOG_PORT" || true
    echo "== FRR のログを送ってきている lab の EC2（rsyslog）=="
    ss -tnp state established "sport = :$LOG_PORT" || true
    echo "== 直近のログ =="
    journalctl -u "$UNIT" -n 20 --no-pager
    ;;
  test)
    # ポーリングだけ 1 回まわして標準出力に出す（MSK には送らない）。機器に届かないときは lab の EC2 の forward を疑う
    [ -f "$CONF" ] || "$SELF" render
    AWS_CONFIG_FILE=/etc/telegraf/aws_config telegraf --config "$CONF" --test --input-filter snmp
    ;;
  logs) journalctl -u "$UNIT" -n "${LINES:-50}" --no-pager ;;
  restart) systemctl restart "$UNIT" && systemctl --no-pager status "$UNIT" ;;
  *) sed -n '2,5p' "$SELF"; exit 1 ;;
esac
