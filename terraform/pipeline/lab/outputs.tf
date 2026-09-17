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
  description = "Run in this repository after downloading the containerlab rpm (README lab-2). Re-run and reboot to change configs. Phase 2 adds the Telegraf rpm (README s-1)."
  value       = "aws s3 sync lab/ s3://${local.bucket}/lab/ --exclude \"wvs2.clab.yml\" && aws s3 cp containerlab_${var.containerlab_version}_linux_arm64.rpm s3://${local.bucket}/lab/"
}

output "upload_telegraf_command" {
  description = "Phase 2. Run after downloading https://dl.influxdata.com/telegraf/releases/telegraf-<telegraf_version>-1.aarch64.rpm, then reboot the instance"
  value       = "aws s3 cp telegraf-${var.telegraf_version}-1.aarch64.rpm s3://${local.bucket}/lab/"
}

output "lab_security_group_id" {
  description = "Read by terraform/pipeline/stream (Kafka 9098 from the lab EC2)"
  value       = aws_security_group.lab.id
}

output "lab_role_name" {
  description = "Read by terraform/pipeline/stream (kafka-cluster write permissions are attached to this role)"
  value       = aws_iam_role.lab.name
}
