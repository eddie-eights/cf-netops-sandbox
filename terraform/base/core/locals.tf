# netops-poc - base root module shared by the three features (pipeline / agent / workflow). VPC with 2 private subnets,
# a NAT Gateway in 1 AZ for egress (no inbound path from the internet), the S3 gateway endpoint, the two security groups
# (internal / endpoints), the chat web EC2 (Gradio: chat + topology figure + device table, 127.0.0.1 only, reached through SSM Session Manager
# port forwarding), the shared S3 bucket and the IAM roles the features attach policies to.
# The AgentCore Runtime, guardrail and optional knowledge base are terraform/agent; the lab / stream / analytics / graph
# roots are the pipeline; Temporal on ECS is terraform/workflow. Each of them reads this state (terraform_remote_state).

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
}
