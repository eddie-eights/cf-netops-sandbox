output "msk_cluster_arn" {
  description = "MSK cluster ARN"
  value       = aws_msk_cluster.stream.arn
}

output "bootstrap_brokers" {
  description = "SASL/IAM bootstrap brokers (also in SSM /<prefix>/msk-bootstrap)"
  value       = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam
}

output "telegraf_cluster_name" {
  description = "ECS cluster of the Telegraf tasks (receiver and poller)"
  value       = aws_ecs_cluster.telegraf.name
}

output "telegraf_service_name" {
  description = "ECS service of the Telegraf receiver (dial-out: traps, syslog, MDT behind the NLB)"
  value       = aws_ecs_service.telegraf.name
}

output "telegraf_poll_service_name" {
  description = "ECS service of the Telegraf poller (dial-in: gNMI subscriptions and SNMP polling)"
  value       = aws_ecs_service.telegraf_poll.name
}

output "telegraf_address" {
  description = "Private IP of the Telegraf NLB (also in SSM /<prefix>/telegraf-address; the DNAT target of lab.sh forward)"
  value       = data.aws_network_interface.telegraf_lb.private_ip
}

output "telegraf_log_group_name" {
  description = "CloudWatch Logs group of the Telegraf tasks (streams dial-out/... and dial-in/...)"
  value       = aws_cloudwatch_log_group.telegraf.name
}

output "telegraf_list_tasks_command" {
  description = "Prints the ARN of the running Telegraf receiver task (dial-out)"
  value       = "aws ecs list-tasks --region ${var.region} --cluster ${aws_ecs_cluster.telegraf.name} --service-name ${aws_ecs_service.telegraf.name} --query taskArns --output text"
}

output "telegraf_poll_list_tasks_command" {
  description = "Prints the ARN of the running Telegraf poller task (dial-in; use its last part as TASK_ID of telegraf_exec_command)"
  value       = "aws ecs list-tasks --region ${var.region} --cluster ${aws_ecs_cluster.telegraf.name} --service-name ${aws_ecs_service.telegraf_poll.name} --query taskArns --output text"
}

output "telegraf_exec_command" {
  description = "Run on the user's PC (AWS CLI v2 + Session Manager plugin) with TASK_ID of the poller task. tg gnmi subscribes for 20 seconds, tg test polls SNMP once (only with snmp_poll = true); neither writes to MSK"
  value       = "aws ecs execute-command --region ${var.region} --cluster ${aws_ecs_cluster.telegraf.name} --task TASK_ID --container telegraf --interactive --command 'tg gnmi'"
}
