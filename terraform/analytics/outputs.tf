output "application_id" {
  description = "EMR Serverless application id (ops/up.sh starts the streaming job on it)"
  value       = aws_emrserverless_application.spark.id
}

output "runtime_role_arn" {
  description = "Execution role passed to start-job-run"
  value       = aws_iam_role.emr.arn
}

output "table_bucket_arn" {
  description = "S3 Tables table bucket (the Iceberg warehouse of the Spark catalog)"
  value       = aws_s3tables_table_bucket.tables.arn
}

output "table_arn" {
  description = "The Iceberg table the streaming job appends to"
  value       = aws_s3tables_table.snmp_metrics.arn
}

output "table_identifier" {
  description = "How Spark SQL names the table (catalog.namespace.table)"
  value       = local.iceberg_table
}

output "script_s3_uri" {
  description = "Where ops/up.sh puts spark/snmp_to_iceberg.py"
  value       = "s3://${local.bucket}/${local.script_key}"
}

output "jars_s3_prefix" {
  description = "Where ops/up.sh puts the Kafka / MSK IAM / S3 Tables jars"
  value       = "s3://${local.bucket}/${local.jars_prefix}/"
}

# start-job-run の引数。ops/up.sh はこれと同じものを組み立てる。手で打つときは README の a-3
output "job_driver_json" {
  description = "jobDriver for start-job-run (script, jars, catalog, Kafka options)"
  value = jsonencode({
    sparkSubmit = {
      entryPoint          = "s3://${local.bucket}/${local.script_key}"
      entryPointArguments = [local.bootstrap, local.iceberg_table, "s3://${local.bucket}/${local.checkpoint}/"]
      sparkSubmitParameters = join(" ", [
        "--conf spark.jars=s3://${local.bucket}/${local.jars_prefix}/*.jar",
        "--conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "--conf spark.sql.catalog.${local.catalog_name}=org.apache.iceberg.spark.SparkCatalog",
        "--conf spark.sql.catalog.${local.catalog_name}.catalog-impl=software.amazon.s3tables.iceberg.S3TablesCatalog",
        "--conf spark.sql.catalog.${local.catalog_name}.warehouse=${aws_s3tables_table_bucket.tables.arn}",
        "--conf spark.driver.cores=1",
        "--conf spark.driver.memory=2g",
        "--conf spark.executor.cores=1",
        "--conf spark.executor.memory=2g",
        "--conf spark.executor.instances=1",
        "--conf spark.dynamicAllocation.enabled=false",
      ])
    }
  })
}

output "configuration_overrides_json" {
  description = "configurationOverrides for start-job-run (driver logs to CloudWatch, worker logs to the asset bucket, no EMR managed storage)"
  value = jsonencode({
    monitoringConfiguration = {
      s3MonitoringConfiguration                 = { logUri = "s3://${local.bucket}/${local.logs_prefix}/" }
      managedPersistenceMonitoringConfiguration = { enabled = false }
      cloudWatchLoggingConfiguration = {
        enabled      = true
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
  description = "See the table (and its metadata location) in S3 Tables"
  value       = "aws s3tables list-tables --region ${var.region} --table-bucket-arn ${aws_s3tables_table_bucket.tables.arn} --namespace ${var.namespace}"
}

output "log_group_name" {
  description = "CloudWatch log group of the job driver"
  value       = aws_cloudwatch_log_group.emr.name
}
