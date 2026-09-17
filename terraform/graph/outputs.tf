output "cluster_endpoint" {
  description = "Writer endpoint (host). Port is 8182."
  value       = aws_neptune_cluster.graph.endpoint
}

output "cluster_resource_id" {
  description = "Used in the neptune-db IAM resource ARN"
  value       = aws_neptune_cluster.graph.cluster_resource_id
}

output "endpoint_parameter_name" {
  description = "SSM parameter the runtime and the web read at start"
  value       = aws_ssm_parameter.endpoint.name
}

output "neptune_security_group_id" {
  description = "Security group of the Neptune cluster (terraform/workflow opens 8182 from the tools Lambda on it)"
  value       = aws_security_group.neptune.id
}

output "next_step" {
  description = "Run on the web EC2 after apply (SSM session), then use the topology tab \"Neptune で編集\" to seed the static data"
  value       = "sudo systemctl restart ${var.name_prefix}-web"
}
