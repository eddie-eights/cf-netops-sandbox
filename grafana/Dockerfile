# Grafana OSS のコンテナ（terraform/pipeline/analytics の ECS。Fargate の arm64）。ops/up.sh の手順 2 が GRAFANA=1 のときビルドして ECR の <接頭辞>-grafana に置く。
# Amazon Managed Grafana は IAM Identity Center（か SAML の IdP）が要り、このアカウントには無いので使えない（2026-09-28）。
# 上流の公式イメージに、AMP と OpenSearch のデータソースのプラグイン・データソースとダッシュボードの定義（provisioning/）・入口（start.sh）を足す。
# タスクは VPC の外へ出られない（NAT が無い）ので、プラグインはビルドのときに入れておく。/var/lib/grafana は上流で VOLUME なので、
# RUN で書いても残らない。/opt/grafana-plugins に入れて GF_PATHS_PLUGINS で指す（RUN があるので x86_64 の PC では QEMU（binfmt）が要る）
ARG GRAFANA_VERSION=13.2.2
FROM grafana/grafana:${GRAFANA_VERSION}

USER root
RUN mkdir -p /opt/grafana-plugins \
 && grafana cli --pluginsDir /opt/grafana-plugins plugins install grafana-amazonprometheus-datasource \
 && grafana cli --pluginsDir /opt/grafana-plugins plugins install grafana-opensearch-datasource \
 && chown -R 472:0 /opt/grafana-plugins
COPY provisioning /etc/grafana/netops
COPY --chmod=0755 start.sh /usr/local/bin/netops-grafana
USER 472

# GF_PLUGINS_PREINSTALL_DISABLED: 起動のたびに grafana.com からプラグインを取りに行かない（届かないので起動が遅れる）。
# 利用統計と更新の確認も外へ出ようとするだけなので切る。SigV4 は AMP と OpenSearch Serverless の認証（タスクロール。鍵は持たない）
ENV GF_PATHS_PLUGINS=/opt/grafana-plugins \
    GF_PLUGINS_PREINSTALL_DISABLED=true \
    GF_ANALYTICS_REPORTING_ENABLED=false \
    GF_ANALYTICS_CHECK_FOR_UPDATES=false \
    GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false \
    GF_NEWS_NEWS_FEED_ENABLED=false \
    GF_SECURITY_DISABLE_GRAVATAR=true \
    GF_USERS_ALLOW_SIGN_UP=false \
    GF_AUTH_SIGV4_AUTH_ENABLED=true \
    GF_AWS_ALLOWED_AUTH_PROVIDERS=default

ENTRYPOINT ["/usr/local/bin/netops-grafana"]
