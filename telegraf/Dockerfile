# Telegraf のコンテナ（terraform/pipeline/stream の ECS。Fargate の arm64）。ops/up.sh の手順 2 がビルドして ECR の <接頭辞>-telegraf に置く。
# 上流の公式イメージに、設定のテンプレート（telegraf.conf.in）、lab の gNMI を共通の形に変える Starlark（lab_*.star）と
# 入口（telegraf.sh → /usr/local/bin/tg）を足すだけ。
# 上流の /entrypoint.sh は root で起きて setpriv で telegraf に落とすが、ここでは初めから telegraf（非 root）で起こす。
# 非 root は 1024 未満のポートで待てないので、trap は 1162 で受ける（NLB が 162 を 1162 に向ける。terraform/pipeline/stream の telegraf.tf）。
# 受け口は trap 1162/udp、syslog 5140/udp、MDT 57000/tcp、outputs.health 8080/tcp
ARG TELEGRAF_VERSION=1.40.0
FROM telegraf:${TELEGRAF_VERSION}

COPY telegraf.conf.in lab_gnmi.star lab_circuits.star /etc/telegraf/
COPY --chmod=0755 telegraf.sh /usr/local/bin/tg

USER telegraf
ENTRYPOINT ["/usr/local/bin/tg"]
CMD ["run"]
