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
  description = "CIDR of the VPC this root module creates (/16 to /24). Two /(n+8) private subnets are cut from it. No internet gateway, no NAT."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/(1[6-9]|2[0-4])$", var.vpc_cidr))
    error_message = "vpc_cidr must be an IPv4 CIDR between /16 and /24."
  }
}

variable "az_id_a" {
  description = "AZ ID of subnet A (chat web EC2, endpoints, Runtime). AZ IDs supported by AgentCore Runtime in Tokyo."
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

variable "client_cidr" {
  description = "Optional. Corporate CIDR whose PCs call the ssm / ssmmessages endpoints of this root module over DX or VPN. Leave empty if PCs reach AWS APIs another way."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^$|^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$", var.client_cidr))
    error_message = "client_cidr must be empty or an IPv4 CIDR."
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
variable "create_ssm_endpoints" {
  description = "Create ssm / ssmmessages interface endpoints. Set false if the VPC already has them."
  type        = bool
  default     = true
}

variable "create_s3_gateway_endpoint" {
  description = "Create the S3 gateway endpoint (free) for ECR layer download. Set false if the route tables already have one."
  type        = bool
  default     = true
}

variable "create_shared_endpoints" {
  description = "Create ecr.api / ecr.dkr / logs interface endpoints (2 AZ). The runtime, the lab EC2, the workflow worker and the Spark job use them. ops/up.sh passes false when only the base (or only graph) is deployed. Set false if the VPC already has them."
  type        = bool
  default     = true
}
