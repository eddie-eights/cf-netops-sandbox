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
  description = "Read by terraform/agent (runtime ENIs), terraform/pipeline/stream (MSK brokers), terraform/pipeline/graph (Neptune subnet group), terraform/pipeline/analytics (EMR, OpenSearch Serverless endpoint) and terraform/workflow (Fargate, Lambda)"
  value       = [aws_subnet.a.id, aws_subnet.b.id]
}

output "instance_subnet_id" {
  description = "Read by terraform/pipeline/lab (the lab and Telegraf EC2 sit next to the chat web)"
  value       = aws_subnet.a.id
}

output "route_table_ids" {
  description = "Read by terraform/pipeline/lab (route of the lab management network to the lab EC2)"
  value       = [aws_route_table.private.id]
}

output "vpc_cidr" {
  description = "CIDR of the VPC (read by terraform/pipeline/analytics for the OpenSearch Serverless network policy comment and by anyone who needs an inside-the-VPC rule)"
  value       = aws_vpc.this.cidr_block
}

output "internal_security_group_id" {
  description = "The one SG every workload wears (inbound from the VPC, outbound free). Read by terraform/agent (runtime ENIs), terraform/pipeline/lab (lab / Telegraf EC2), terraform/pipeline/stream (MSK brokers), terraform/pipeline/graph (Neptune, status Lambda), terraform/pipeline/analytics (EMR) and terraform/workflow (Fargate, tools Lambda)"
  value       = aws_security_group.internal.id
}

output "endpoint_security_group_id" {
  description = "SG of the VPC endpoints (HTTPS from internal_security_group_id). Read by terraform/pipeline/analytics (OpenSearch Serverless VPC endpoint)"
  value       = aws_security_group.endpoints.id
}

output "kb_bucket_name" {
  description = "Read by terraform/agent (docs/), terraform/pipeline/lab (lab/ telegraf/) and terraform/pipeline/analytics (analytics/)"
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
  description = "Run in this repository after \"pip download\" into wheels/ (step 4 of ops/up.sh). Copies every web/*.py (app / config / chat / topology_view / incident_view) plus the agent modules the web UI shares (toolkit / topology / anomalies / graph / proposals). The instance pulls web/ on every boot."
  value       = "aws s3 cp web/ s3://${aws_s3_bucket.kb.bucket}/web/ --recursive --exclude '*' --include '*.py' --include 'requirements.txt' && for f in toolkit topology anomalies graph proposals; do aws s3 cp agent/$f.py s3://${aws_s3_bucket.kb.bucket}/web/$f.py; done && aws s3 cp agent/data/ s3://${aws_s3_bucket.kb.bucket}/web/data/ --recursive && aws s3 sync wheels/ s3://${aws_s3_bucket.kb.bucket}/web/wheels/"
}
