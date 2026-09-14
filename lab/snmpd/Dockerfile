FROM alpine:3.20
RUN apk add --no-cache net-snmp net-snmp-tools
EXPOSE 161/udp
# -C: 既定の設定ファイルを読まない  -f: フォアグラウンド  -Lo: stdout にログ
CMD ["snmpd", "-f", "-Lo", "-C", "-c", "/etc/snmp/snmpd.conf"]
