# Splunk Enterprise のコンテナ（terraform/pipeline/analytics の ECS。Fargate の x86_64）。ops/up.sh の手順 2 が SINK_SPLUNK=1 のときビルドして
# ECR の <接頭辞>-splunk に置く。上流の公式イメージ（splunk/splunk。amd64 だけ）に、アラートの app（netops_alerts）と入口（entrypoint.sh）を足すだけ。
# 上流は etc を /opt/splunk-etc に持ち、起動のとき /opt/splunk/etc（VOLUME）へ写すので、app は /opt/splunk-etc/apps に置く
# （/opt/splunk/etc に COPY しても VOLUME に隠れる）。RUN は無いので arm64 の PC でも QEMU 無しでビルドできる（--platform linux/amd64）。
# Splunkbase の add-on（Splunk Add-on for AWS など）は入れていない（配布物をこの公開リポジトリに置けず、VPC から Splunkbase へも出られない）
ARG SPLUNK_VERSION=10.4.3
FROM splunk/splunk:${SPLUNK_VERSION}

COPY --chown=splunk:splunk netops_alerts /opt/splunk-etc/apps/netops_alerts
COPY --chmod=0755 entrypoint.sh /sbin/nwc-entrypoint.sh

# ENTRYPOINT を替えると上流の CMD は消えるので書き直す
ENTRYPOINT ["/sbin/nwc-entrypoint.sh"]
CMD ["start-service"]
