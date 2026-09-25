# ---------------------------------------------------------------- network
# Spark のワーカーの SG は terraform/base/core の internal（受信は VPC の中から、送信は自由）。EMR Serverless は受信に 0.0.0.0/0 が開いた SG を拒む
# （AWS ドキュメント「Configuring VPC access」、2026-09-17 確認）ので、internal の受信は VPC の CIDR で書いてある。
# MSK の 9098 と Neptune の 8182 は同じ SG の中なので穴は要らない。S3 / S3 Tables のデータは S3 gateway エンドポイント、
# s3tables の API・CloudWatch Logs・EventBridge・Prometheus・Splunk の HEC は NAT Gateway から出る。
# 2026-09-26 まではここに EMR の SG（11 本のルール）と s3tables / events のエンドポイントがあった（7c42b0f）。
# stream / graph の state が読めるかは emr.tf の precondition で見る
