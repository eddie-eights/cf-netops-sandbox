FROM alpine:3.20
# 社内の証明書で apk が落ちるときは certs/ に社内のルート証明書（.crt / .pem）を置く（docs/pipeline.md の「動かないとき」。イメージには残らない）
RUN --mount=type=bind,source=certs,target=/certs \
    set -eu; \
    cp /etc/ssl/certs/ca-certificates.crt /tmp/ca.orig; \
    for c in /certs/*.crt /certs/*.pem; do [ -f "$c" ] && cat "$c" >> /etc/ssl/certs/ca-certificates.crt; done; \
    apk add --no-cache net-snmp net-snmp-tools; \
    mv /tmp/ca.orig /etc/ssl/certs/ca-certificates.crt
EXPOSE 161/udp
# -C: 既定の設定ファイルを読まない  -f: フォアグラウンド  -Lo: stdout にログ
CMD ["snmpd", "-f", "-Lo", "-C", "-c", "/etc/snmp/snmpd.conf"]
