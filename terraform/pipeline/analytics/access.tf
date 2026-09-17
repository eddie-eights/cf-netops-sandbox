# ---------------------------------------------------------------- runtime role of the Spark job
resource "aws_iam_role" "emr" {
  name        = "${var.name_prefix}-emr-runtime"
  description = "EMR Serverless job runtime - reads MSK, writes the sinks (S3 Tables, OpenSearch Serverless, Prometheus), reads the script and jars from the asset bucket"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "emr-serverless.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:emr-serverless:${var.region}:${local.account_id}:/applications/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "emr" {
  name = "${var.name_prefix}-emr-runtime"
  role = aws_iam_role.emr.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        # スクリプトと jar を読む。checkpoint とログを書く
        Sid      = "AssetBucket"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${local.bucket_arn}/${local.s3_prefix}/*"
      },
      {
        Sid      = "AssetBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = local.bucket_arn
      },
      {
        # Kafka を読む（consumer group は spark-kafka-source-* で Spark が付ける）
        Sid      = "KafkaCluster"
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
        Resource = local.msk_cluster_arn
      },
      {
        Sid      = "KafkaTopics"
        Effect   = "Allow"
        Action   = ["kafka-cluster:DescribeTopic", "kafka-cluster:ReadData"]
        Resource = local.topic_arns
      },
      {
        Sid      = "KafkaGroups"
        Effect   = "Allow"
        Action   = ["kafka-cluster:DescribeGroup", "kafka-cluster:AlterGroup"]
        Resource = local.group_arns
      },
      {
        Sid      = "DriverLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "${aws_cloudwatch_log_group.emr.arn}:*"
      },
      {
        Sid      = "DriverLogsGroups"
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "*"
      },
      {
        # 検知（spark/snmp_sinks.py の detect）: 異常テーブルに open / resolved を書く
        Sid      = "AnomalyTable"
        Effect   = "Allow"
        Action   = "dynamodb:UpdateItem"
        Resource = local.anomaly_table_arn
      },
      {
        # 新しい異常を EventBridge の既定のバスに出す（terraform/workflow の events.tf が受ける）
        Sid      = "AnomalyEvents"
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = local.event_bus_arn
      },
      ],
      # ---- 格納先ごと（sinks.tf。選んだものだけ）
      # 「cond ? [..] : []」は両辺の型が揃わず validate が落ちるので for … if で絞る
      [for s in [{
        # Iceberg のカタログ操作（S3 Tables の API）。テーブルは Terraform が作るが、Spark はメタデータの場所を読み書きする
        Sid    = "S3TablesCatalog"
        Effect = "Allow"
        Action = [
          "s3tables:GetTableBucket",
          "s3tables:ListNamespaces",
          "s3tables:GetNamespace",
          "s3tables:ListTables",
          "s3tables:GetTable",
          "s3tables:GetTableMetadataLocation",
          "s3tables:UpdateTableMetadataLocation",
          "s3tables:GetTableData",
          "s3tables:PutTableData",
        ]
        Resource = local.sink_iceberg ? [
          aws_s3tables_table_bucket.tables[0].arn,
          "${aws_s3tables_table_bucket.tables[0].arn}/table/*",
        ] : []
      }] : s if local.sink_iceberg],
      [for s in [{
        # コレクションの API（中身の権限はデータアクセスポリシー aws_opensearchserverless_access_policy.logs）
        Sid      = "OpenSearchCollection"
        Effect   = "Allow"
        Action   = "aoss:APIAccessAll"
        Resource = local.sink_opensearch ? aws_opensearchserverless_collection.logs[0].arn : ""
      }] : s if local.sink_opensearch],
      [for s in [{
        Sid      = "PrometheusRemoteWrite"
        Effect   = "Allow"
        Action   = "aps:RemoteWrite"
        Resource = local.sink_prometheus ? aws_prometheus_workspace.metrics[0].arn : ""
      }] : s if local.sink_prometheus],
    )
  })
}
