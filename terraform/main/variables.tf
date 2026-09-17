# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/ecr and terraform/agent."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Prefix for resource names and the value of the Project tag (up to 25 characters). Same value as terraform/ecr and terraform/agent."
  type        = string
  default     = "fukuda-nwc-poc"

  validation {
    # OpenSearch Serverless のコレクション名（name_prefix-kb）が 28 文字までなので 25 文字に抑える
    condition     = can(regex("^[a-z][a-z0-9-]{1,24}$", var.name_prefix))
    error_message = "name_prefix must match ^[a-z][a-z0-9-]{1,24}$."
  }
}

variable "owner" {
  description = "Value of the owner tag. Same value as terraform/ecr and terraform/agent."
  type        = string
  default     = "fukuda"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.owner))
    error_message = "owner must match ^[A-Za-z0-9._-]{1,64}$."
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
