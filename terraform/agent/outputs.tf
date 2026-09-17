output "agent_runtime_arn" {
  description = "ARN of the AgentCore Runtime (read by terraform/workflow; the chat web reads it from runtime_arn_parameter_name)"
  value       = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
}

output "agent_runtime_id" {
  description = "ID of the AgentCore Runtime (used in the log group name)"
  value       = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_id
}

output "runtime_log_group_name" {
  description = "Created by AgentCore, not by Terraform. ops/up.sh sets retention and the Project tag, ops/down.sh deletes it."
  value       = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.agent.agent_runtime_id}-DEFAULT"
}

output "runtime_arn_parameter_name" {
  description = "SSM parameter the chat web reads the runtime ARN from (60 second cache, no reboot needed)"
  value       = aws_ssm_parameter.runtime_arn.name
}

output "guardrail_id" {
  description = "Guardrail ID passed to the runtime"
  value       = aws_bedrock_guardrail.this.guardrail_id
}

output "guardrail_version" {
  description = "Guardrail version passed to the runtime"
  value       = aws_bedrock_guardrail_version.r1.version
}

# ---------------------------------------------------------------- knowledge base (empty strings when create_knowledge_base = false)
output "knowledge_base_id" {
  description = "Bedrock knowledge base ID (the runtime reads it from KNOWLEDGE_BASE_ID). Empty without create_knowledge_base."
  value       = try(aws_bedrockagent_knowledge_base.kb[0].id, "")
}

output "data_source_id" {
  description = "Data source ID for start-ingestion-job. Empty without create_knowledge_base."
  value       = try(aws_bedrockagent_data_source.docs[0].data_source_id, "")
}

output "collection_endpoint" {
  description = "OpenSearch Serverless collection endpoint (the vector index lives here). Empty without create_knowledge_base."
  value       = try(aws_opensearchserverless_collection.kb[0].collection_endpoint, "")
}

output "upload_docs_command" {
  description = "Run in this repository to upload the sample markdown files (create_knowledge_base = true)"
  value       = "aws s3 cp kb-docs/ s3://${local.bucket_name}/docs/ --recursive --exclude \"*\" --include \"*.md\""
}

output "start_ingestion_command" {
  description = "Run after uploading or changing the files. The knowledge base does not sync by itself. Empty without create_knowledge_base."
  value       = local.kb ? "aws bedrock-agent start-ingestion-job --region ${var.region} --knowledge-base-id ${aws_bedrockagent_knowledge_base.kb[0].id} --data-source-id ${aws_bedrockagent_data_source.docs[0].data_source_id}" : ""
}
