output "agent_repository_url" {
  description = "Push the agent image here (docs/deploy-manual.md step 2). terraform/base/core reads it from this state."
  value       = aws_ecr_repository.agent.repository_url
}

output "frr_repository_url" {
  description = "Push quay.io/frrouting/frr:10.2.1 here with tag 10.2.1 (docs/pipeline.md lab-1). Empty when create_lab_repositories is false."
  value       = try(aws_ecr_repository.lab["frr"].repository_url, "")
}

output "snmpd_repository_url" {
  description = "Build lab/snmpd and push it here with tag v1 (docs/pipeline.md lab-1)."
  value       = try(aws_ecr_repository.lab["snmpd"].repository_url, "")
}

output "multitool_repository_url" {
  description = "Push ghcr.io/srl-labs/network-multitool:v0.10.0 here with tag v0.10.0 (docs/pipeline.md lab-1)."
  value       = try(aws_ecr_repository.lab["multitool"].repository_url, "")
}

output "worker_repository_url" {
  description = "Build workflow/ and push it here with the same tag as the agent image (docs/workflow.md w-1). Empty when create_workflow_repositories is false."
  value       = try(aws_ecr_repository.workflow["worker"].repository_url, "")
}

output "temporal_repository_url" {
  description = "Push temporalio/temporal:1.9.1 here with tag 1.9.1 (docs/workflow.md w-1)."
  value       = try(aws_ecr_repository.workflow["temporal"].repository_url, "")
}
