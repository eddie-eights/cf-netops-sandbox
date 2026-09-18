# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/base/core."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Prefix for resource names and the value of the Project tag (up to 22 characters). Same value as terraform/base/core."
  type        = string
  default     = "netops-poc"

  validation {
    # 一番きついのは OpenSearch Serverless の data access policy 名（32 文字まで）で、一番長い接尾辞は terraform/workflow の <name_prefix>-logs-read（10 文字）。だから 22 文字に抑える
    # ハイフンの連続と末尾のハイフンも弾く（ECR のリポジトリ名が受け付けない）
    condition     = can(regex("^[a-z][a-z0-9]*(-[a-z0-9]+)*$", var.name_prefix)) && length(var.name_prefix) >= 2 && length(var.name_prefix) <= 22
    error_message = "name_prefix must be 2-22 lowercase letters, digits and single hyphens, starting with a letter and not ending with one."
  }
}

variable "owner" {
  description = "Value of the owner tag. Same value as terraform/base/core."
  type        = string
  default     = "netops"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.owner))
    error_message = "owner must match ^[A-Za-z0-9._-]{1,64}$."
  }
}

# ---------------------------------------------------------------- agent
variable "agent_image_tag" {
  description = "Tag pushed to the agent repository of terraform/base/ecr (docs/deploy-manual.md step 2). The repository URL is read from terraform/base/ecr/terraform.tfstate."
  type        = string
  default     = "v1"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.agent_image_tag))
    error_message = "agent_image_tag must match ^[A-Za-z0-9._-]{1,128}$."
  }
}

variable "agent_image_uri" {
  description = "Optional. Leave empty to use <terraform/base/ecr repository>:<agent_image_tag>. Set only to run an image from another repository, with tag, built for linux/arm64 (e.g. 123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/netops-poc-agent:v1)."
  type        = string
  default     = ""
}

variable "runtime_name" {
  description = "Optional. AgentCore Runtime name (letters, digits and underscore only. Hyphens are not allowed). Leave empty to derive it from name_prefix: hyphens become underscores and _agent is appended (netops-poc -> netops_poc_agent)."
  type        = string
  default     = ""

  validation {
    # 空なら locals.tf が name_prefix から作る（^[a-z][a-z0-9-]{1,22}$ から作るので必ずこの形に収まる）
    condition     = var.runtime_name == "" || can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}$", var.runtime_name))
    error_message = "runtime_name must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$."
  }
}

variable "model_id" {
  description = "Bedrock model or inference profile ID (jp.amazon.nova-2-lite-v1:0 runs in Tokyo and Osaka)."
  type        = string
  default     = "jp.amazon.nova-2-lite-v1:0"
}

# ---------------------------------------------------------------- knowledge base (optional) and guardrail
variable "create_knowledge_base" {
  description = "Create the Bedrock Knowledge Base (S3 -> Titan Embeddings v2 -> OpenSearch Serverless) and pass it to the runtime. Off by default: the collection costs about 0.33 USD per hour (1 OCU with standby disabled). Without it the agent answers from the model, the topology tools and the MCP tools only."
  type        = bool
  default     = false
}

variable "kb_admin_principal_arn" {
  description = "Optional (create_knowledge_base = true). IAM role or user ARN whose credentials run this apply (not an sts assumed-role ARN). It is added to the OpenSearch Serverless data access policy so Terraform can create the vector index. Leave empty to derive it from the caller (assumed-role sessions resolve to the role ARN)."
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
  description = "Candidate chunks retrieved by the hybrid search per question (before reranking). Used only with create_knowledge_base = true."
  type        = number
  default     = 20

  validation {
    condition     = var.number_of_results >= 1 && var.number_of_results <= 100
    error_message = "number_of_results must be between 1 and 100."
  }
}

variable "rerank_model_id" {
  description = "Reranker model ID (amazon.rerank-v1:0). Leave empty to disable reranking. Used only with create_knowledge_base = true."
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

# ---------------------------------------------------------------- existing VPC endpoints
variable "create_runtime_endpoints" {
  description = "Create the bedrock-runtime interface endpoint (2 AZ). ecr.api / ecr.dkr / logs are created by terraform/base/core (create_shared_endpoints). Set false if the VPC already has it."
  type        = bool
  default     = true
}

variable "create_kb_endpoint" {
  description = "Create the bedrock-agent-runtime interface endpoint (the runtime calls Retrieve through it). Only with create_knowledge_base = true. Set false if the VPC already has it."
  type        = bool
  default     = true
}

variable "create_agentcore_endpoint" {
  description = "Create the bedrock-agentcore interface endpoint (the chat web and the workflow worker invoke the runtime through it). Set false if the VPC already has it."
  type        = bool
  default     = true
}
