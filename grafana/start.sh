#!/bin/sh
# Grafana のコンテナ（grafana/Dockerfile）の入口。選んだ格納先のデータソースとダッシュボードだけを /tmp に並べてから、上流の /run.sh を起こす。
# ECS のタスク定義の環境変数（terraform/pipeline/analytics の grafana.tf）:
#   PROMETHEUS_URL    AMP のワークスペース（https://aps-workspaces.<region>.amazonaws.com/workspaces/<id>）。空なら Prometheus を出さない（SINK_PROMETHEUS=0）
#   OPENSEARCH_URL    logs コレクションのエンドポイント。空なら OpenSearch を出さない（SINK_OPENSEARCH=0）
#   OPENSEARCH_INDEX  ログの index（spark/snmp_sinks.py の OPENSEARCH_INDEX）
#   AWS_REGION
# 管理者のパスワード GF_SECURITY_ADMIN_PASSWORD は ECS が SSM の SecureString から入れる（値はここでもログでも出さない）
set -eu
SRC=/etc/grafana/netops
DST=/tmp/grafana-provisioning
rm -rf "$DST" /tmp/grafana-dashboards
mkdir -p "$DST/datasources" "$DST/dashboards" "$DST/plugins" "$DST/alerting" /tmp/grafana-dashboards
cp "$SRC/dashboards/netops.yaml" "$DST/dashboards/"
if [ -n "${PROMETHEUS_URL:-}" ]; then
  cp "$SRC/datasources/prometheus.yaml" "$DST/datasources/"
  cp "$SRC/dashboards/metrics.json" /tmp/grafana-dashboards/
fi
if [ -n "${OPENSEARCH_URL:-}" ]; then
  cp "$SRC/datasources/opensearch.yaml" "$DST/datasources/"
  cp "$SRC/dashboards/logs.json" /tmp/grafana-dashboards/
fi
export GF_PATHS_PROVISIONING="$DST"
echo "provisioning: $(ls "$DST/datasources" /tmp/grafana-dashboards | tr '\n' ' ')"
exec /run.sh "$@"
