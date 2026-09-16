output "web_instance_id" {
  description = "Target of the SSM port forwarding session"
  value       = aws_instance.web.id
}

output "start_session_command" {
  description = "Run on the user's PC (AWS CLI v2 + Session Manager plugin, macOS / Linux quoting), keep it running, then open chat_url (Gradio - chat tab and topology tab)"
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.web.id} --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"8080\"],\"localPortNumber\":[\"8080\"]}'"
}

output "chat_url" {
  description = "Open in the browser while the session above is running"
  value       = "http://localhost:8080/"
}

output "agent_runtime_arn" {
  description = "ARN of the AgentCore Runtime (the chat web invokes it)"
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

# ---------------------------------------------------------------- read by terraform/lab, terraform/stream, terraform/graph (terraform_remote_state)
output "vpc_id" {
  description = "Read by terraform/lab / terraform/stream / terraform/graph"
  value       = aws_vpc.this.id
}

output "runtime_subnet_ids" {
  description = "Read by terraform/stream (MSK brokers) and terraform/graph (Neptune subnet group)"
  value       = [aws_subnet.a.id, aws_subnet.b.id]
}

output "instance_subnet_id" {
  description = "Read by terraform/lab (the lab EC2 sits next to the chat web)"
  value       = aws_subnet.a.id
}

output "route_table_ids" {
  description = "Read by terraform/stream (DynamoDB gateway endpoint)"
  value       = [aws_route_table.private.id]
}

output "instance_security_group_id" {
  description = "Allow 443 from this SG on existing ssm / ssmmessages / bedrock-agentcore endpoints"
  value       = aws_security_group.web.id
}

output "runtime_security_group_id" {
  description = "Allow 443 from this SG on existing ecr / logs / bedrock-runtime / bedrock-agent-runtime endpoints"
  value       = aws_security_group.runtime.id
}

output "endpoint_security_group_id" {
  description = "Read by terraform/lab / terraform/stream, which add 443 ingress rules so the lab EC2 and MSK Connect can reach the ecr / ssm endpoints created here"
  value       = aws_security_group.endpoints.id
}

output "kb_bucket_name" {
  description = "Read by terraform/lab (lab/) and terraform/stream (stream/)"
  value       = aws_s3_bucket.kb.bucket
}

output "kb_bucket_arn" {
  description = "ARN of the bucket (read by terraform/lab / terraform/stream for IAM policies)"
  value       = aws_s3_bucket.kb.arn
}

output "runtime_role_name" {
  description = "Read by terraform/stream / terraform/graph, which attach read policies for the anomaly table and Neptune"
  value       = aws_iam_role.runtime.name
}

output "web_role_name" {
  description = "Read by terraform/stream / terraform/graph"
  value       = aws_iam_role.web.name
}

# ---------------------------------------------------------------- commands
output "upload_web_command" {
  description = "Run in this repository after \"pip download\" into wheels/ (README step 4). Also copies the agent modules the web UI shares (topology / anomalies / graph / proposals). The instance pulls web/ on every boot."
  value       = "aws s3 cp web/app.py s3://${aws_s3_bucket.kb.bucket}/web/app.py && aws s3 cp web/requirements.txt s3://${aws_s3_bucket.kb.bucket}/web/requirements.txt && for f in topology anomalies graph proposals; do aws s3 cp agent/$f.py s3://${aws_s3_bucket.kb.bucket}/web/$f.py; done && aws s3 cp agent/data/ s3://${aws_s3_bucket.kb.bucket}/web/data/ --recursive && aws s3 sync wheels/ s3://${aws_s3_bucket.kb.bucket}/web/wheels/"
}

output "upload_docs_command" {
  description = "Run in this repository to upload the sample markdown files"
  value       = "aws s3 cp kb-docs/ s3://${aws_s3_bucket.kb.bucket}/docs/ --recursive --exclude \"*\" --include \"*.md\""
}

output "start_ingestion_command" {
  description = "Run after uploading or changing the files. The knowledge base does not sync by itself."
  value       = "aws bedrock-agent start-ingestion-job --region ${var.region} --knowledge-base-id ${aws_bedrockagent_knowledge_base.kb.id} --data-source-id ${aws_bedrockagent_data_source.docs.data_source_id}"
}

output "knowledge_base_id" {
  description = "Bedrock knowledge base ID (the runtime reads it from KNOWLEDGE_BASE_ID)"
  value       = aws_bedrockagent_knowledge_base.kb.id
}

output "data_source_id" {
  description = "Data source ID for start-ingestion-job"
  value       = aws_bedrockagent_data_source.docs.data_source_id
}

output "collection_endpoint" {
  description = "OpenSearch Serverless collection endpoint (the vector index lives here)"
  value       = aws_opensearchserverless_collection.kb.collection_endpoint
}

output "guardrail_id" {
  description = "Guardrail ID passed to the runtime"
  value       = aws_bedrock_guardrail.this.guardrail_id
}

output "guardrail_version" {
  description = "Guardrail version passed to the runtime"
  value       = aws_bedrock_guardrail_version.r1.version
}
