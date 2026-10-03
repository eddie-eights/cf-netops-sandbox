#!/usr/bin/env bash
# lab の EC2 の支度。起動のたびに user_data が S3 の lab/ を置き直してからこれを呼ぶ。user_data は 2 つあり、どちらも
# /etc/<接頭辞>-lab.env を書いて S3 から置き直し、これを exec するだけ（中身はここに 1 つだけ。tests/test_lab_debug.py が見る）:
#   terraform/pipeline/lab/templates/lab_user_data.sh.tftpl   lab の EC2（Telegraf は stream の ECS。TELEGRAF_IMAGE は空）
#   cloudformation/lab-debug.yaml の UserData                 デバッグ用の EC2（Telegraf もこの EC2 で動かす。TELEGRAF_IMAGE がある）
# env のキー: NAME_PREFIX / AWS_REGION / REGISTRY / SRLINUX_IMAGE / MULTITOOL_IMAGE / TELEGRAF_IMAGE / PARAM_PREFIX / CONTAINERLAB_VERSION / AUTO_START_LAB
set -euo pipefail
SRC=$(dirname "$(readlink -f "$0")")
ENV_FILE=$(ls /etc/*-lab.env 2>/dev/null | head -1 || true)
[ -n "$ENV_FILE" ] || { echo "/etc/*-lab.env が無い（user_data が書く）" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
: "${NAME_PREFIX:?}" "${CONTAINERLAB_VERSION:?}" "${AUTO_START_LAB:?}"
RPM=$SRC/containerlab_${CONTAINERLAB_VERSION}_linux_arm64.rpm

# Docker は AL2023 のリポジトリから（S3 ゲートウェイエンドポイント経由）。containerlab は S3 に置いた rpm から
command -v docker >/dev/null || dnf install -y docker
# snmpwalk は lab.sh check / snmp が EC2 から機器の ifTable を引くのに使う。入らなくてもトポロジは上げる
command -v snmpwalk >/dev/null || dnf install -y net-snmp-utils || echo "net-snmp-utils could not be installed. \"lab snmp\" will not work." >&2
systemctl enable --now docker
# VM（linux ノード）の bond0（LACP。leaf の組へ dual-home）はカーネルの bonding モジュールが要る。コンテナからは読み込めないので EC2 側で。再起動後も lab.sh up が modprobe する
echo bonding > /etc/modules-load.d/nwc-lab-bonding.conf
modprobe bonding || echo "bonding module could not be loaded. The VMs cannot build bond0." >&2
if [ ! -f "$SRC/lab.sh" ] || [ ! -f "$RPM" ]; then
  echo "lab/ or the containerlab rpm is not in S3 (lab/) yet. Run ops/up.sh (or ops/lab-debug.sh up) and reboot." >&2
  exit 0
fi
command -v containerlab >/dev/null || dnf install -y "$RPM"
chmod 0755 "$SRC/lab.sh"
ln -sfn "$SRC/lab.sh" /usr/local/bin/lab
# 前の版は Telegraf の rpm をこの EC2 に入れていた。今は入れない（lab の EC2 は stream の ECS、デバッグ用の EC2 は ECR の同じイメージを docker で動かす）

cat > "/etc/systemd/system/$NAME_PREFIX-lab.service" <<__UNIT__
[Unit]
Description=$NAME_PREFIX containerlab topology (splab, Spine-Leaf)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$SRC/lab.sh up
ExecStop=$SRC/lab.sh down
# SR Linux は 6 台が並んで起きるのに数分かかる（containerlab は 1 台 5 分まで待つ）
TimeoutStartSec=1200

[Install]
WantedBy=multi-user.target
__UNIT__

# デバッグ用の EC2 だけ: Telegraf（stream の ECS と同じイメージ・同じ telegraf.conf.in。出力は標準出力）を host ネットワークで動かす（lab.sh telegraf）
TG_UNIT=/etc/systemd/system/$NAME_PREFIX-telegraf.service
if [ -n "${TELEGRAF_IMAGE:-}" ]; then
  cat > "$TG_UNIT" <<__UNIT__
[Unit]
Description=$NAME_PREFIX Telegraf for the lab (stdout. docker logs telegraf)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$SRC/lab.sh telegraf run
ExecStop=$SRC/lab.sh telegraf stop

[Install]
WantedBy=multi-user.target
__UNIT__
else
  systemctl disable --now "$NAME_PREFIX-telegraf.service" 2>/dev/null || true
  rm -f "$TG_UNIT"
fi

systemctl daemon-reload
"$SRC/lab.sh" render
"$SRC/lab.sh" pull
if [ "$AUTO_START_LAB" = "true" ]; then
  systemctl enable "$NAME_PREFIX-lab.service"
  systemctl restart "$NAME_PREFIX-lab.service"
else
  systemctl disable "$NAME_PREFIX-lab.service" || true
fi
if [ -n "${TELEGRAF_IMAGE:-}" ]; then
  systemctl enable "$NAME_PREFIX-telegraf.service"
  systemctl restart "$NAME_PREFIX-telegraf.service"
fi
