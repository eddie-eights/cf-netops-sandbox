output "application_id" {
  description = "EMR Serverless application id (ops/up.sh starts the streaming job on it)"
  value       = aws_emrserverless_application.spark.id
}

output "runtime_role_arn" {
  description = "Execution role passed to start-job-run"
  value       = aws_iam_role.emr.arn
}

output "table_bucket_arn" {
  description = "S3 Tables table bucket (the Iceberg warehouse of the Spark catalog). Empty unless sinks has iceberg"
  value       = local.table_bucket_arn
}

output "table_arn" {
  description = "The Iceberg table the streaming job appends to. Empty unless sinks has iceberg"
  value       = local.sink_iceberg ? aws_s3tables_table.snmp_metrics[0].arn : ""
}

output "table_identifier" {
  description = "How Spark SQL names the table (catalog.namespace.table). Empty unless sinks has iceberg"
  value       = local.sink_iceberg ? local.iceberg_table : ""
}

output "script_s3_uri" {
  description = "Where ops/up.sh puts spark/snmp_sinks.py"
  value       = "s3://${local.bucket}/${local.script_key}"
}

output "jars_s3_prefix" {
  description = "Where ops/up.sh puts the Kafka / MSK IAM / S3 Tables jars"
  value       = "s3://${local.bucket}/${local.jars_prefix}/"
}

# start-job-run の引数。ops/up.sh はこの出力をそのまま --job-driver に渡す（手順 7-5）
output "job_driver_json" {
  description = "jobDriver for start-job-run (script, sinks and their endpoints, jars, catalog)"
  value = jsonencode({
    sparkSubmit = {
      entryPoint = "s3://${local.bucket}/${local.script_key}"
      # 「cond ? [..] : []」は両辺の型が揃わず validate が落ちるので for … if で絞る
      entryPointArguments = concat(
        ["--bootstrap", local.bootstrap, "--checkpoint", local.checkpoint_uri, "--sinks", join(",", var.sinks), "--region", var.region,
          "--metric-topics", local.metric_topics, "--log-topics", local.log_topics,
          # Source を接頭辞ごとに変える。既定のバスは 1 つの AWS アカウントで共有なので、ここを固定にすると
          # 他の人の異常が自分の workflow / graph のルールに当たる（ルール名は接頭辞付きでも event_pattern は別）
        "--anomaly-table", local.anomaly_table, "--device-map", var.device_map, "--event-bus", var.event_bus, "--event-source", local.event_source],
        [for a in ["--iceberg-table", local.iceberg_table] : a if local.sink_iceberg],
        [for a in ["--opensearch-endpoint", local.opensearch_endpoint, "--opensearch-index", local.opensearch_index] : a if local.sink_opensearch],
        [for a in ["--prometheus-url", local.prometheus_remote_write_url] : a if local.sink_prometheus],
      )
      # Iceberg のカタログの設定は iceberg があるときだけ（warehouse のテーブルバケットが無いとカタログを開けない）
      sparkSubmitParameters = join(" ", concat(
        ["--conf spark.jars=s3://${local.bucket}/${local.jars_prefix}/*.jar"],
        [for c in [
          "--conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
          "--conf spark.sql.catalog.${local.catalog_name}=org.apache.iceberg.spark.SparkCatalog",
          "--conf spark.sql.catalog.${local.catalog_name}.catalog-impl=software.amazon.s3tables.iceberg.S3TablesCatalog",
          "--conf spark.sql.catalog.${local.catalog_name}.warehouse=${local.table_bucket_arn}",
        ] : c if local.sink_iceberg],
        ["--conf spark.driver.cores=1",
          "--conf spark.driver.memory=2g",
          "--conf spark.executor.cores=1",
          "--conf spark.executor.memory=2g",
          "--conf spark.executor.instances=1",
        "--conf spark.dynamicAllocation.enabled=false"],
      ))
    }
  })
}

output "configuration_overrides_json" {
  description = "configurationOverrides for start-job-run (driver logs to CloudWatch, worker logs to the asset bucket, no EMR managed storage)"
  value = jsonencode({
    monitoringConfiguration = {
      s3MonitoringConfiguration                 = { logUri = "s3://${local.bucket}/${local.logs_prefix}/" }
      managedPersistenceMonitoringConfiguration = { enabled = false }
      # logs のインターフェイスエンドポイントは terraform/agent が作る。無い VPC で有効にすると、ジョブが
      # 「Unable to push logs ... Connect timeout on endpoint URL: https://logs...」で FAILED になる（2026-09-17 に AGENT=0 で実測）
      cloudWatchLoggingConfiguration = {
        enabled      = var.cloudwatch_logging
        logGroupName = aws_cloudwatch_log_group.emr.name
        logTypes     = { SPARK_DRIVER = ["stdout", "stderr"] }
      }
    }
  })
}

output "list_job_runs_command" {
  description = "See whether the streaming job is running"
  value       = "aws emr-serverless list-job-runs --region ${var.region} --application-id ${aws_emrserverless_application.spark.id} --query 'jobRuns[].[id,state,name]' --output table"
}

output "list_tables_command" {
  description = "See the table (and its metadata location) in S3 Tables. Empty unless sinks has iceberg"
  value       = local.sink_iceberg ? "aws s3tables list-tables --region ${var.region} --table-bucket-arn ${local.table_bucket_arn} --namespace ${var.namespace}" : ""
}

output "log_group_name" {
  description = "CloudWatch log group of the job driver"
  value       = aws_cloudwatch_log_group.emr.name
}

output "sinks" {
  description = "Where the streaming job stores the messages (var.sinks: iceberg = all topics, opensearch = log topics, prometheus = metric topics)"
  value       = var.sinks
}

output "opensearch_collection_endpoint" {
  description = "OpenSearch Serverless collection the log topics go to (empty unless sinks has opensearch). Reachable only from inside the VPC"
  value       = local.opensearch_endpoint
}

output "prometheus_workspace_id" {
  description = "Amazon Managed Service for Prometheus workspace the metric topics go to (empty unless sinks has prometheus)"
  value       = local.sink_prometheus ? aws_prometheus_workspace.metrics[0].id : ""
}

output "prometheus_remote_write_url" {
  description = "remote write URL the job posts to (empty unless sinks has prometheus)"
  value       = local.prometheus_remote_write_url
}

output "anomaly_table_name" {
  description = "DynamoDB table the detection query writes (from terraform/pipeline/stream)"
  value       = local.anomaly_table
}

output "opensearch_collection_name" {
  description = "Name of the OpenSearch Serverless collection (terraform/workflow adds a read-only data access policy for the tools). Empty unless sinks has opensearch"
  value       = local.sink_opensearch ? aws_opensearchserverless_collection.logs[0].name : ""
}

output "opensearch_collection_arn" {
  description = "ARN of the OpenSearch Serverless collection (aoss:APIAccessAll for the tools Lambda). Empty unless sinks has opensearch"
  value       = local.sink_opensearch ? aws_opensearchserverless_collection.logs[0].arn : ""
}

output "opensearch_index" {
  description = "Index the log topics go to"
  value       = local.opensearch_index
}

output "prometheus_workspace_arn" {
  description = "ARN of the Prometheus workspace (aps:QueryMetrics for the tools Lambda). Empty unless sinks has prometheus"
  value       = local.sink_prometheus ? aws_prometheus_workspace.metrics[0].arn : ""
}

output "events_endpoint_id" {
  description = "events interface endpoint the driver puts AnomalyOpened through"
  value       = aws_vpc_endpoint.events.id
}

output "prometheus_query_url" {
  description = "Query endpoint of the workspace (PromQL over HTTP with SigV4, or Grafana data source). Empty unless sinks has prometheus"
  value       = local.sink_prometheus ? "${aws_prometheus_workspace.metrics[0].prometheus_endpoint}api/v1/query" : ""
}
