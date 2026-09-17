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

# ---------------------------------------------------------------- read by terraform/agent, terraform/pipeline/lab, terraform/pipeline/stream, terraform/pipeline/analytics, terraform/pipeline/graph, terraform/workflow (terraform_remote_state)
output "vpc_id" {
  description = "Read by terraform/agent / terraform/pipeline/lab / terraform/pipeline/stream / terraform/pipeline/graph / terraform/workflow"
  value       = aws_vpc.this.id
}

output "runtime_subnet_ids" {
  description = "Read by terraform/agent (runtime ENIs and endpoints), terraform/pipeline/stream (MSK brokers) and terraform/pipeline/graph (Neptune subnet group)"
  value       = [aws_subnet.a.id, aws_subnet.b.id]
}

output "instance_subnet_id" {
  description = "Read by terraform/pipeline/lab (the lab EC2 sits next to the chat web) and terraform/agent (bedrock-agentcore endpoint)"
  value       = aws_subnet.a.id
}

output "route_table_ids" {
  description = "Read by terraform/pipeline/stream (DynamoDB gateway endpoint)"
  value       = [aws_route_table.private.id]
}

output "instance_security_group_id" {
  description = "Allow 443 from this SG on existing ssm / ssmmessages / bedrock-agentcore endpoints"
  value       = aws_security_group.web.id
}

output "runtime_security_group_id" {
  description = "Read by terraform/agent (the runtime ENIs). Allow 443 from this SG on existing ecr / logs / bedrock-runtime / bedrock-agent-runtime endpoints"
  value       = aws_security_group.runtime.id
}

output "endpoint_security_group_id" {
  description = "Read by terraform/agent (its interface endpoints use this SG) and terraform/pipeline/lab / terraform/pipeline/stream, which add 443 ingress rules so the lab EC2 and MSK Connect can reach the endpoints"
  value       = aws_security_group.endpoints.id
}

output "kb_bucket_name" {
  description = "Read by terraform/agent (docs/), terraform/pipeline/lab (lab/) and terraform/pipeline/stream (stream/)"
  value       = aws_s3_bucket.kb.bucket
}

output "kb_bucket_arn" {
  description = "ARN of the bucket (read by terraform/agent / terraform/pipeline/lab / terraform/pipeline/stream for IAM policies)"
  value       = aws_s3_bucket.kb.arn
}

output "runtime_role_name" {
  description = "Read by terraform/agent (execution policy), terraform/pipeline/stream / terraform/pipeline/graph (read policies for the anomaly table and Neptune)"
  value       = aws_iam_role.runtime.name
}

output "runtime_role_arn" {
  description = "Read by terraform/agent (role_arn of the AgentCore Runtime)"
  value       = aws_iam_role.runtime.arn
}

output "web_role_name" {
  description = "Read by terraform/agent (InvokeAgentRuntime) / terraform/pipeline/stream / terraform/pipeline/graph"
  value       = aws_iam_role.web.name
}

# ---------------------------------------------------------------- commands
output "upload_web_command" {
  description = "Run in this repository after \"pip download\" into wheels/ (README step 4). Also copies the agent modules the web UI shares (topology / anomalies / graph / proposals). The instance pulls web/ on every boot."
  value       = "aws s3 cp web/app.py s3://${aws_s3_bucket.kb.bucket}/web/app.py && aws s3 cp web/requirements.txt s3://${aws_s3_bucket.kb.bucket}/web/requirements.txt && for f in topology anomalies graph proposals; do aws s3 cp agent/$f.py s3://${aws_s3_bucket.kb.bucket}/web/$f.py; done && aws s3 cp agent/data/ s3://${aws_s3_bucket.kb.bucket}/web/data/ --recursive && aws s3 sync wheels/ s3://${aws_s3_bucket.kb.bucket}/web/wheels/"
}
