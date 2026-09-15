output "msk_cluster_arn" {
  description = "MSK cluster ARN"
  value       = aws_msk_cluster.stream.arn
}

output "bootstrap_brokers" {
  description = "SASL/IAM bootstrap brokers (also in SSM /<name_prefix>/msk-bootstrap)"
  value       = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam
}

output "anomaly_table_name" {
  description = "DynamoDB table of the anomaly list (also in SSM /<name_prefix>/anomaly-table)"
  value       = aws_dynamodb_table.anomalies.name
}

output "msk_security_group_id" {
  description = "Security group of the brokers, MSK Connect workers and the Lambda ESM ENIs"
  value       = aws_security_group.msk.id
}

output "upload_plugin_command" {
  description = "Upload command of the Confluent S3 sink zip (README s-1). The zip must be there before the first apply when create_s3_sink is true."
  value       = "aws s3 cp ${basename(var.s3_sink_plugin_key)} s3://${local.bucket}/${var.s3_sink_plugin_key}"
}

output "sink_prefix" {
  description = "Where MSK Connect writes the raw messages"
  value       = "s3://${local.bucket}/stream/"
}

output "detector_logs" {
  description = "Follow the detector Lambda logs"
  value       = "aws logs tail /aws/lambda/${var.name_prefix}-detector --region ${var.region} --follow"
}
