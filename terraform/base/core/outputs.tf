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
  description = "Read by terraform/agent (runtime ENIs), terraform/pipeline/stream (MSK brokers, Telegraf task and NLB), terraform/pipeline/graph (Neptune subnet group), terraform/pipeline/analytics (EMR, Grafana / Splunk tasks) and terraform/workflow (Fargate, Lambda)"
  value       = [aws_subnet.a.id, aws_subnet.b.id]
}

output "instance_subnet_id" {
  description = "Read by terraform/pipeline/lab (the lab EC2 sits next to the chat web)"
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

output "security_group_ids" {
  description = "SG of each workload (security_groups.tf), keyed web / lab / telegraf / telegraf_nlb / msk / spark / grafana / splunk / neptune / lambda / workflow / runtime / endpoints. Read by terraform/agent (runtime, lambda), terraform/pipeline/lab (lab), terraform/pipeline/stream (telegraf, telegraf_nlb, msk), terraform/pipeline/graph (neptune, lambda), terraform/pipeline/analytics (spark, grafana, splunk) and terraform/workflow (workflow, lambda)"
  value       = local.sg_ids
}

output "opensearch_vpc_endpoint_id" {
  description = "ID of the OpenSearch Serverless VPC endpoint, empty unless create_opensearch_endpoint. Read by terraform/agent (knowledge base network policy) and terraform/pipeline/analytics (logs network policy)"
  value       = try(aws_opensearchserverless_vpc_endpoint.aoss[0].id, "")
}

output "network_perimeter" {
  description = "True when the Deny outside aws:SourceVpc is on (perimeter.tf). Read by the other roots to add their resource policy Deny statements"
  value       = var.network_perimeter
}

output "network_perimeter_policy_arn" {
  description = "IAM policy that denies AWS API calls not coming through a VPC endpoint, empty unless network_perimeter. Attached by terraform/pipeline/lab, terraform/pipeline/analytics and terraform/workflow to their workload roles"
  value       = var.network_perimeter ? aws_iam_policy.network_perimeter.arn : ""
}

output "perimeter_exempt_principals" {
  description = "Principals the resource policy Deny statements leave out (the deployer and the knowledge base role). Used by the alerts topic here (alerts.tf) and read by terraform/pipeline/analytics (S3 Tables) and terraform/workflow (SQS)"
  value       = local.perimeter_exempt_principals
}

output "alerts_topic_arn" {
  description = "SNS topic the Grafana alert rules and the Splunk saved searches publish to (alerts.tf). Read by terraform/pipeline/analytics (publish), terraform/workflow (SQS subscription) and terraform/pipeline/graph (Lambda subscription)"
  value       = aws_sns_topic.alerts.arn
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
  description = "Read by terraform/agent (execution policy), terraform/pipeline/stream (SSM parameters) and terraform/pipeline/graph (read policy for Neptune)"
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

output "flow_log_group_name" {
  description = "VPC flow logs (flow_logs.tf). Query REJECT with CloudWatch Logs Insights (docs/troubleshooting.md)"
  value       = aws_cloudwatch_log_group.flow_logs.name
}

# ---------------------------------------------------------------- commands
output "upload_web_command" {
  description = "Run in this repository after \"pip download\" into wheels/ (step 4 of ops/up.sh). Copies every web/*.py (app / config / chat / topology_view / incident_view) plus the agent modules the web UI shares (toolkit / topology / graph / proposals). The instance pulls web/ on every boot."
  value       = "aws s3 cp web/ s3://${aws_s3_bucket.kb.bucket}/web/ --recursive --exclude '*' --include '*.py' --include 'requirements.txt' && for f in toolkit topology graph proposals; do aws s3 cp agent/$f.py s3://${aws_s3_bucket.kb.bucket}/web/$f.py; done && aws s3 cp agent/data/ s3://${aws_s3_bucket.kb.bucket}/web/data/ --recursive && aws s3 sync wheels/ s3://${aws_s3_bucket.kb.bucket}/web/wheels/"
}
