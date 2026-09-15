# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/ecr."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Prefix for resource names and the value of the Project tag (up to 25 characters). Same value as terraform/ecr."
  type        = string
  default     = "fukuda-nwc-poc"

  validation {
    # OpenSearch Serverless のコレクション名（name_prefix-kb）が 28 文字までなので 25 文字に抑える
    condition     = can(regex("^[a-z][a-z0-9-]{1,24}$", var.name_prefix))
    error_message = "name_prefix must match ^[a-z][a-z0-9-]{1,24}$."
  }
}

variable "owner" {
  description = "Value of the owner tag. Same value as terraform/ecr."
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

# ---------------------------------------------------------------- agent
variable "agent_image_tag" {
  description = "Tag pushed to the agent repository of terraform/ecr (README step 2). The repository URL is read from terraform/ecr/terraform.tfstate."
  type        = string
  default     = "v1"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.agent_image_tag))
    error_message = "agent_image_tag must match ^[A-Za-z0-9._-]{1,128}$."
  }
}

variable "agent_image_uri" {
  description = "Optional. Leave empty to use <terraform/ecr repository>:<agent_image_tag>. Set only to run an image from another repository, with tag, built for linux/arm64 (e.g. 123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/fukuda-nwc-poc-agent:v1)."
  type        = string
  default     = ""
}

variable "runtime_name" {
  description = "AgentCore Runtime name (letters, digits and underscore only. Hyphens are not allowed)."
  type        = string
  default     = "fukuda_nwc_poc_agent"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}$", var.runtime_name))
    error_message = "runtime_name must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$."
  }
}

variable "model_id" {
  description = "Bedrock model or inference profile ID (jp.amazon.nova-2-lite-v1:0 runs in Tokyo and Osaka)."
  type        = string
  default     = "jp.amazon.nova-2-lite-v1:0"
}

# ---------------------------------------------------------------- knowledge base and guardrail
variable "kb_admin_principal_arn" {
  description = "Optional. IAM role or user ARN whose credentials run this apply (not an sts assumed-role ARN). It is added to the OpenSearch Serverless data access policy so Terraform can create the vector index. Leave empty to derive it from the caller (assumed-role sessions resolve to the role ARN)."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^$|^arn:aws:iam::[0-9]{12}:(role|user)/.+$", var.kb_admin_principal_arn))
    error_message = "kb_admin_principal_arn must be empty or arn:aws:iam::<account>:(role|user)/<name>."
  }
}

variable "guardrail_profile_id" {
  description = "System-defined guardrail profile for cross-Region inference (required by the Standard tier). apac.guardrail.v1:0 for Tokyo."
  type        = string
  default     = "apac.guardrail.v1:0"

  validation {
    condition     = can(regex("^[a-z0-9-]+[.]guardrail[.]v[0-9:]+$", var.guardrail_profile_id))
    error_message = "guardrail_profile_id must look like apac.guardrail.v1:0."
  }
}

variable "number_of_results" {
  description = "Candidate chunks retrieved by the hybrid search per question (before reranking)."
  type        = number
  default     = 20

  validation {
    condition     = var.number_of_results >= 1 && var.number_of_results <= 100
    error_message = "number_of_results must be between 1 and 100."
  }
}

variable "rerank_model_id" {
  description = "Reranker model ID (amazon.rerank-v1:0). Leave empty to disable reranking."
  type        = string
  default     = "amazon.rerank-v1:0"

  validation {
    condition     = can(regex("^$|^[a-z0-9-]+[.]rerank-v[0-9-]+:[0-9]+$", var.rerank_model_id))
    error_message = "rerank_model_id must be empty or look like amazon.rerank-v1:0."
  }
}

variable "number_of_reranked_results" {
  description = "Chunks kept after reranking and passed to the model (ignored when rerank_model_id is empty)."
  type        = number
  default     = 5

  validation {
    condition     = var.number_of_reranked_results >= 1 && var.number_of_reranked_results <= 100
    error_message = "number_of_reranked_results must be between 1 and 100."
  }
}

variable "opensearch_cacert_file" {
  description = "Optional. Path of a PEM file with the CA that signs HTTPS on this PC (corporate SSL inspection). Passed to the opensearch provider that creates the vector index. The aws provider reads AWS_CA_BUNDLE instead."
  type        = string
  default     = ""
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
variable "create_runtime_endpoints" {
  description = "Create ecr.api / ecr.dkr / logs / bedrock-runtime interface endpoints. Set false if the VPC already has them."
  type        = bool
  default     = true
}

variable "create_kb_endpoint" {
  description = "Create the bedrock-agent-runtime interface endpoint (the runtime calls Retrieve through it). Set false if the VPC already has it."
  type        = bool
  default     = true
}

variable "create_ssm_endpoints" {
  description = "Create ssm / ssmmessages interface endpoints. Set false if the VPC already has them."
  type        = bool
  default     = true
}

variable "create_agentcore_endpoint" {
  description = "Create the bedrock-agentcore interface endpoint (the chat web invokes the runtime through it). Set false if the VPC already has it."
  type        = bool
  default     = true
}

variable "create_s3_gateway_endpoint" {
  description = "Create the S3 gateway endpoint (free) for ECR layer download. Set false if the route tables already have one."
  type        = bool
  default     = true
}
