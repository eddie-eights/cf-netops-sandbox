output "lab_instance_id" {
  description = "Target of the SSM session"
  value       = aws_instance.lab.id
}

output "start_session_command" {
  description = "Run on the user's PC (AWS CLI v2 + Session Manager plugin), then \"sudo lab check\" / \"sudo lab failover\" / \"sudo lab status\""
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.lab.id}"
}

output "stop_command" {
  description = "Stop the instance when not in use (no compute charge while stopped; the EBS volume is still charged)"
  value       = "aws ec2 stop-instances --region ${var.region} --instance-ids ${aws_instance.lab.id}"
}

output "start_command" {
  description = "Start it again. The topology is deployed at boot when auto_start_lab is true."
  value       = "aws ec2 start-instances --region ${var.region} --instance-ids ${aws_instance.lab.id}"
}

output "upload_lab_command" {
  description = "Run in this repository after downloading the containerlab rpm (step 5 of ops/up.sh). Re-run and reboot to change configs."
  value       = "aws s3 sync lab/ s3://${local.bucket}/lab/ --exclude \"wanlab.clab.yml\" && aws s3 cp containerlab_${var.containerlab_version}_linux_arm64.rpm s3://${local.bucket}/lab/"
}

# ---- Telegraf EC2（create_telegraf が false なら空）
output "telegraf_instance_id" {
  description = "Target of the SSM session to the Telegraf EC2. Empty without create_telegraf."
  value       = try(aws_instance.telegraf[0].id, "")
}

output "telegraf_start_session_command" {
  description = "Run on the user's PC, then \"sudo tg status\" / \"sudo tg test\" / \"sudo tg logs\""
  value       = var.create_telegraf ? "aws ssm start-session --region ${var.region} --target ${aws_instance.telegraf[0].id}" : ""
}

output "telegraf_address" {
  description = "Fixed private IP of the Telegraf EC2 (also in SSM /<prefix>/telegraf-address for lab.sh forward)"
  value       = try(aws_network_interface.telegraf[0].private_ip, "")
}

output "upload_telegraf_command" {
  description = "Run in this repository after downloading https://dl.influxdata.com/telegraf/releases/telegraf-<telegraf_version>-1.aarch64.rpm (step 5 of ops/up.sh), then reboot the Telegraf EC2"
  value       = "aws s3 sync telegraf/ s3://${local.bucket}/telegraf/ && aws s3 cp telegraf-${var.telegraf_version}-1.aarch64.rpm s3://${local.bucket}/telegraf/"
}

output "telegraf_security_group_id" {
  description = "Read by terraform/pipeline/stream (Kafka 9098 from the Telegraf EC2). Empty without create_telegraf."
  value       = try(aws_security_group.telegraf[0].id, "")
}

output "telegraf_role_name" {
  description = "Read by terraform/pipeline/stream (kafka-cluster write permissions are attached to this role). Empty without create_telegraf."
  value       = try(aws_iam_role.telegraf[0].name, "")
}
