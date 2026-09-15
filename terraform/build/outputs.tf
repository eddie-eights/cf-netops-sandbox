output "source_bucket_name" {
  description = "Upload src.zip (agent/ and lab/snmpd/) here before starting a build."
  value       = aws_s3_bucket.source.bucket
}

output "project_name" {
  description = "CodeBuild project. Start it from the console (Start build) or with the commands below."
  value       = aws_codebuild_project.build.name
}

output "start_agent_build_command" {
  description = "Builds and pushes the agent image (change v1 for every update)."
  value       = "aws codebuild start-build --region ${var.region} --project-name ${aws_codebuild_project.build.name} --environment-variables-override name=TARGET,value=agent name=IMAGE_TAG,value=v1"
}

output "start_lab_build_command" {
  description = "Pulls frr / multitool and builds snmpd, then pushes the three lab images."
  value       = "aws codebuild start-build --region ${var.region} --project-name ${aws_codebuild_project.build.name} --environment-variables-override name=TARGET,value=lab name=IMAGE_TAG,value=v1"
}
