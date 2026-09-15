output "agent_repository_url" {
  description = "Push the agent image here (README step 2). terraform/main reads it from this state."
  value       = aws_ecr_repository.agent.repository_url
}

output "frr_repository_url" {
  description = "Push quay.io/frrouting/frr:10.2.1 here with tag 10.2.1 (README lab-1). Empty when create_lab_repositories is false."
  value       = try(aws_ecr_repository.lab["frr"].repository_url, "")
}

output "snmpd_repository_url" {
  description = "Build lab/snmpd and push it here with tag v1 (README lab-1)."
  value       = try(aws_ecr_repository.lab["snmpd"].repository_url, "")
}

output "multitool_repository_url" {
  description = "Push ghcr.io/srl-labs/network-multitool:v0.10.0 here with tag v0.10.0 (README lab-1)."
  value       = try(aws_ecr_repository.lab["multitool"].repository_url, "")
}
