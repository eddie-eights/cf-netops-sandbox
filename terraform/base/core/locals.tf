# netops-poc - base root module shared by the three features (pipeline / agent / workflow). VPC without NAT, EIP,
# public IP or load balancer (2 private subnets, S3 gateway endpoint, ssm / ssmmessages endpoints), the security groups,
# the chat web EC2 (Gradio: chat + topology figure + device table, 127.0.0.1 only, reached through SSM Session Manager
# port forwarding), the shared S3 bucket and the IAM roles the features attach policies to.
# The AgentCore Runtime, guardrail and optional knowledge base are terraform/agent; the lab / stream / analytics / graph
# roots are the pipeline; Temporal on ECS is terraform/workflow. Each of them reads this state (terraform_remote_state).

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
}
