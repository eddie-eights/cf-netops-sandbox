# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/base/ecr and terraform/agent."
  type        = string
  default     = "ap-northeast-1"
}

variable "owner" {
  description = "Required. Name of the person who deploys this copy. Resource names and the Project tag are <owner>-nwc-poc, so each person can find their own resources in the console. Same value in every root module."
  type        = string

  validation {
    # 接頭辞は <owner>-nwc-poc。OpenSearch Serverless の data access policy 名が 32 文字までで、一番長い接尾辞は terraform/workflow の <接頭辞>-logs-read（10 文字）なので接頭辞は 22 文字まで。-nwc-poc の 8 文字を引いて owner は 14 文字まで
    # ハイフンの連続と末尾のハイフンも弾く（ECR のリポジトリ名が受け付けない）
    condition     = can(regex("^[a-z][a-z0-9]*(-[a-z0-9]+)*$", var.owner)) && length(var.owner) <= 14
    error_message = "owner must be 1-14 lowercase letters, digits and single hyphens, starting with a letter and not ending with one."
  }
}

# ---------------------------------------------------------------- network
variable "vpc_cidr" {
  description = "CIDR of the VPC this root module creates (/16 to /24). Two /(n+8) private subnets are cut from it."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/(1[6-9]|2[0-4])$", var.vpc_cidr))
    error_message = "vpc_cidr must be an IPv4 CIDR between /16 and /24."
  }
}

variable "az_id_a" {
  description = "AZ ID of subnet A (chat web EC2, lab, Runtime). AZ IDs supported by AgentCore Runtime in Tokyo."
  type        = string
  default     = "apne1-az1"

  validation {
    condition     = contains(["apne1-az1", "apne1-az2", "apne1-az4"], var.az_id_a)
    error_message = "az_id_a must be apne1-az1, apne1-az2 or apne1-az4."
  }
}

variable "az_id_b" {
  description = "AZ ID of subnet B (Runtime needs two AZs). Must differ from az_id_a."
  type        = string
  default     = "apne1-az4"

  validation {
    condition     = contains(["apne1-az1", "apne1-az2", "apne1-az4"], var.az_id_b) && var.az_id_b != var.az_id_a
    error_message = "az_id_b must be apne1-az1, apne1-az2 or apne1-az4 and differ from az_id_a."
  }
}

# ---------------------------------------------------------------- chat web (EC2)
variable "instance_type" {
  description = "Chat web EC2. Gradio with numpy and pandas needs about 400 MB of memory, so t4g.small (2 GB) is the default."
  type        = string
  default     = "t4g.small"

  validation {
    condition     = contains(["t4g.micro", "t4g.small", "t4g.medium"], var.instance_type)
    error_message = "instance_type must be t4g.micro, t4g.small or t4g.medium (arm64)."
  }
}

variable "ami_ssm_parameter" {
  description = "SSM public parameter that resolves to the AMI. Amazon Linux 2023 arm64 (SSM Agent and AWS CLI v2 are preinstalled)."
  type        = string
  default     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# ---------------------------------------------------------------- existing VPC endpoints
variable "create_s3_gateway_endpoint" {
  description = "Create the S3 gateway endpoint (free; S3 / S3 Tables / ECR layers / the AL2023 dnf repositories are reached through it - there is no NAT Gateway). Set false if the route tables already have one."
  type        = bool
  default     = true
}

variable "create_opensearch_endpoint" {
  description = "Create the OpenSearch Serverless VPC endpoint (endpoints.tf). One per VPC serves every collection: the knowledge base of terraform/agent and the logs collection of terraform/pipeline/analytics, both reachable only through it. ops/up.sh sets true with CREATE_KB or SINK_OPENSEARCH. About 0.03 USD/h (2 AZ)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------- network perimeter (endpoints.tf, perimeter.tf)
variable "interface_endpoints" {
  description = "Interface VPC endpoints to create (the part after com.amazonaws.<region>.). Every AWS API the workloads call goes through one of these (there is no other way out of the VPC), so the perimeter (perimeter.tf) can require aws:SourceVpc. ops/up.sh builds the list from the features (and from roots that still have resources). About 0.014 USD/h per AZ each."
  type        = list(string)
  default     = ["ssm", "ssmmessages"]

  validation {
    condition = alltrue([for s in var.interface_endpoints : contains([
      "ssm", "ssmmessages", "ecr.api", "ecr.dkr", "logs",
      "bedrock-runtime", "bedrock-agent-runtime", "bedrock-agentcore", "bedrock-agentcore.gateway",
      "s3tables", "events", "aps-workspaces", "sqs",
    ], s)])
    error_message = "interface_endpoints may only list ssm, ssmmessages, ecr.api, ecr.dkr, logs, bedrock-runtime, bedrock-agent-runtime, bedrock-agentcore, bedrock-agentcore.gateway, s3tables, events, aps-workspaces and sqs."
  }
}

variable "endpoints_multi_az" {
  description = "Put the interface endpoints in both subnets (production). false puts them in subnet a only - workloads in subnet b still reach them through the private DNS (cross-AZ), at half the hourly price."
  type        = bool
  default     = false
}

variable "flow_log_retention_days" {
  description = "Retention of the VPC flow log group /<prefix>/vpc-flow-logs (flow_logs.tf)"
  type        = number
  default     = 7
}

variable "network_perimeter" {
  description = "Deny AWS API calls that do not come through this VPC (aws:SourceVpc): an IAM policy on the workload roles (perimeter.tf, attached by every root) and resource policies on the bucket here, the S3 Tables bucket, the SQS queues and the AgentCore Runtime and Gateway in the other roots (Prometheus has no resource policy - only the IAM side). The deployer (whoever runs terraform) and AWS service principals are excepted. false only to rule it out while troubleshooting."
  type        = bool
  default     = true
}
