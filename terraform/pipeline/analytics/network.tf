# ---------------------------------------------------------------- network
# Spark のワーカーの SG は terraform/base/core の spark。受信は自分自身から（driver と executor）だけ、送信は MSK の 9098・Neptune の 8182・
# Splunk の HEC 8088・エンドポイントと S3 の 443（security_groups.tf の通信の表）。EMR Serverless は受信に 0.0.0.0/0 が開いた SG を拒む
# （AWS ドキュメント「Configuring VPC access」、2026-09-17 確認）が、spark の受信は SG の参照だけ。S3 / S3 Tables のデータは S3 gateway エンドポイント、
# s3tables の API・CloudWatch Logs・EventBridge・Prometheus（aps-workspaces）は terraform/base/core のインターフェース型エンドポイント
# （ops/up.sh が PIPELINE のときに作らせる）。VPC から AWS の外へ出る経路は無い（Splunk も ECS で VPC の中）。
# 2026-09-26 まではここに EMR の SG（11 本のルール）と s3tables / events のエンドポイントがあった（7c42b0f）。2026-09-28 から base/core に置く。
# stream / graph の state が読めるかは emr.tf の precondition で見る
