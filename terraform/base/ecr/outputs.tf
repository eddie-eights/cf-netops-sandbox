output "agent_repository_url" {
  description = "Push the agent image here (step 2 of ops/up.sh). terraform/base/core reads it from this state."
  value       = aws_ecr_repository.agent.repository_url
}

output "srlinux_repository_url" {
  description = "Push ghcr.io/nokia/srlinux:26.7.2 (arm64) here with tag 26.7.2 (step 2 of ops/up.sh). Empty when create_lab_repositories is false."
  value       = try(aws_ecr_repository.lab["srlinux"].repository_url, "")
}

output "multitool_repository_url" {
  description = "Push ghcr.io/srl-labs/network-multitool:v0.10.0 here with tag v0.10.0 (step 2 of ops/up.sh)."
  value       = try(aws_ecr_repository.lab["multitool"].repository_url, "")
}

output "worker_repository_url" {
  description = "Build workflow/ and push it here with the same tag as the agent image (step 2 of ops/up.sh). Empty when create_workflow_repositories is false."
  value       = try(aws_ecr_repository.workflow["worker"].repository_url, "")
}

output "temporal_repository_url" {
  description = "Push temporalio/temporal:1.9.1 here with tag 1.9.1 (step 2 of ops/up.sh)."
  value       = try(aws_ecr_repository.workflow["temporal"].repository_url, "")
}
