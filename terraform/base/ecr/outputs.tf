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

output "telegraf_repository_url" {
  description = "Build telegraf/ (arm64) and push it here with tag <Telegraf version>-<hash of telegraf/> (step 2 of ops/up.sh). terraform/pipeline/stream runs it on ECS."
  value       = aws_ecr_repository.pipeline["telegraf"].repository_url
}

output "grafana_repository_url" {
  description = "Build grafana/ (arm64) and push it here with tag <Grafana version>-<hash of grafana/> (step 2 of ops/up.sh, GRAFANA=1). terraform/pipeline/analytics runs it on ECS."
  value       = aws_ecr_repository.pipeline["grafana"].repository_url
}

output "splunk_repository_url" {
  description = "ops/up.sh builds splunk/ (splunk/splunk plus the netops_alerts app, amd64 only) and pushes it here as <Splunk version>-<hash of splunk/> (step 2, SINK_SPLUNK=1). terraform/pipeline/analytics runs it on ECS."
  value       = aws_ecr_repository.pipeline["splunk"].repository_url
}

output "nautobot_repository_url" {
  description = "ops/up.sh builds nautobot/ (networktocode/nautobot plus the NetOps jobs, arm64) and pushes it here as <Nautobot version>-<hash of the build context> (step 2, NAUTOBOT=1). terraform/pipeline/nautobot runs it on ECS."
  value       = aws_ecr_repository.pipeline["nautobot"].repository_url
}

output "redis_repository_url" {
  description = "Push redis:<REDIS_TAG of ops/up.sh> (arm64) here with the same tag (step 2, NAUTOBOT=1). The Redis sidecar of the Nautobot task (cache and Celery broker)."
  value       = aws_ecr_repository.pipeline["redis"].repository_url
}
