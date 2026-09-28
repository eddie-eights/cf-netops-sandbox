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
  value       = "aws s3 sync lab/ s3://${local.bucket}/lab/ --exclude \"splab.clab.yml\" && aws s3 cp containerlab_${var.containerlab_version}_linux_arm64.rpm s3://${local.bucket}/lab/"
}
