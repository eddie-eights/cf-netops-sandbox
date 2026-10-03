output "msk_cluster_arn" {
  description = "MSK cluster ARN"
  value       = aws_msk_cluster.stream.arn
}

output "bootstrap_brokers" {
  description = "SASL/IAM bootstrap brokers (also in SSM /<prefix>/msk-bootstrap)"
  value       = aws_msk_cluster.stream.bootstrap_brokers_sasl_iam
}

output "telegraf_cluster_name" {
  description = "ECS cluster of the Telegraf tasks (dialout and dialin)"
  value       = aws_ecs_cluster.telegraf.name
}

output "telegraf_dialout_service_name" {
  description = "ECS service of the Telegraf dial-out task (traps, syslog, MDT behind the NLB)"
  value       = aws_ecs_service.telegraf_dialout.name
}

output "telegraf_dialin_service_name" {
  description = "ECS service of the Telegraf dial-in task (gNMI subscriptions and SNMP polling). The Nautobot job forces a new deployment of it after it rewrites the targets."
  value       = aws_ecs_service.telegraf_dialin.name
}

output "telegraf_dialin_target_parameters" {
  description = "SSM parameters (String) holding the targets of the dial-in task, keyed gnmi-targets / snmp-agents. Under .../nautobot/ when dialin_targets_from_nautobot (the Nautobot job writes them), else .../lab/."
  value       = local.dialin_target_names
}

output "telegraf_dialin_targets_source" {
  description = "Where the dial-in targets come from: lab or nautobot"
  value       = local.dialin_target_source
}

output "telegraf_address" {
  description = "Private IP of the Telegraf dial-out NLB (also in SSM /<prefix>/telegraf-address; the DNAT target of lab.sh forward)"
  value       = data.aws_network_interface.telegraf_dialout_lb.private_ip
}

output "telegraf_log_group_name" {
  description = "CloudWatch Logs group of the Telegraf tasks (streams dialout/... and dialin/...)"
  value       = aws_cloudwatch_log_group.telegraf.name
}

output "telegraf_dialout_list_tasks_command" {
  description = "Prints the ARN of the running Telegraf dial-out task"
  value       = "aws ecs list-tasks --region ${var.region} --cluster ${aws_ecs_cluster.telegraf.name} --service-name ${aws_ecs_service.telegraf_dialout.name} --query taskArns --output text"
}

output "telegraf_dialin_list_tasks_command" {
  description = "Prints the ARN of the running Telegraf dial-in task (use its last part as TASK_ID of telegraf_exec_command)"
  value       = "aws ecs list-tasks --region ${var.region} --cluster ${aws_ecs_cluster.telegraf.name} --service-name ${aws_ecs_service.telegraf_dialin.name} --query taskArns --output text"
}

output "telegraf_exec_command" {
  description = "Run on the user's PC (AWS CLI v2 + Session Manager plugin) with TASK_ID of the dial-in task. tg gnmi subscribes for 20 seconds, tg test polls SNMP once (only with snmp_poll = true); neither writes to MSK"
  value       = "aws ecs execute-command --region ${var.region} --cluster ${aws_ecs_cluster.telegraf.name} --task TASK_ID --container telegraf --interactive --command 'tg gnmi'"
}
