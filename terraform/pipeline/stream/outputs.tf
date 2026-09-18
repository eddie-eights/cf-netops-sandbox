output "msk_cluster_arn" {
  description = "MSK cluster ARN"
  value       = aws_msk_cluster.stream.arn
}

output "bootstrap_brokers" {
  description = "SASL/IAM bootstrap brokers (also in SSM /<prefix>/msk-bootstrap)"
  value       = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam
}

output "anomaly_table_name" {
  description = "DynamoDB table of the anomaly list (also in SSM /<prefix>/anomaly-table)"
  value       = aws_dynamodb_table.anomalies.name
}

output "msk_security_group_id" {
  description = "Security group of the brokers and MSK Connect workers (terraform/pipeline/analytics opens 9098 from the EMR workers on it)"
  value       = aws_security_group.msk.id
}

output "upload_plugin_command" {
  description = "Upload command of the Confluent S3 sink zip (docs/pipeline.md の s-1). The zip must be there before the first apply when create_s3_sink is true."
  value       = "aws s3 cp ${basename(var.s3_sink_plugin_key)} s3://${local.bucket}/${var.s3_sink_plugin_key}"
}

output "sink_prefix" {
  description = "Where MSK Connect writes the raw messages"
  value       = "s3://${local.bucket}/stream/"
}

output "anomaly_table_arn" {
  description = "ARN of the anomaly table (terraform/pipeline/analytics lets the Spark job write it, terraform/workflow lets the tools Lambda read it)"
  value       = aws_dynamodb_table.anomalies.arn
}
