output "cluster_name" {
  description = "ECS cluster of the workflow task"
  value       = aws_ecs_cluster.workflow.name
}

output "service_name" {
  description = "ECS service (desired_count 1). ops/up.sh waits for it with aws ecs wait services-stable."
  value       = aws_ecs_service.workflow.name
}

output "proposal_table_name" {
  description = "DynamoDB table of the proposals (web tab 承認 reads and decides here)"
  value       = aws_dynamodb_table.proposals.name
}

output "anomaly_queue_url" {
  description = "SQS queue the AnomalyOpened events land in (the worker long-polls it)"
  value       = aws_sqs_queue.anomalies.url
}

output "anomaly_rule_name" {
  description = "EventBridge rule that routes AnomalyOpened (source <name_prefix>.spark) to the queue"
  value       = aws_cloudwatch_event_rule.anomalies.name
}

output "tools_function_name" {
  description = "Tools Lambda behind the gateway (empty when create_gateway is false)"
  value       = try(aws_lambda_function.tools[0].function_name, "")
}

output "task_role_name" {
  description = "IAM role of the worker"
  value       = aws_iam_role.task.name
}

output "gateway_id" {
  description = "AgentCore Gateway id (empty when create_gateway is false)"
  value       = try(aws_bedrockagentcore_gateway.tools[0].gateway_id, "")
}

output "gateway_url" {
  description = "MCP endpoint of the gateway (also in SSM <name_prefix>/gateway-url)"
  value       = try(aws_bedrockagentcore_gateway.tools[0].gateway_url, "")
}

output "worker_logs_command" {
  description = "Tail the worker and Temporal logs"
  value       = "aws logs tail ${aws_cloudwatch_log_group.workflow.name} --region ${var.region} --follow"
}

output "exec_command" {
  description = "Open a shell in the worker container (ECS Exec). Replace TASK_ID with the id from aws ecs list-tasks."
  value       = "aws ecs execute-command --region ${var.region} --cluster ${aws_ecs_cluster.workflow.name} --task TASK_ID --container worker --interactive --command /bin/sh"
}

output "list_tasks_command" {
  description = "Task ids of the service"
  value       = "aws ecs list-tasks --region ${var.region} --cluster ${aws_ecs_cluster.workflow.name} --service-name ${aws_ecs_service.workflow.name} --query taskArns --output text"
}
