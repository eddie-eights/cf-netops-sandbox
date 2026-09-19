# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/base/core."
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

# ---------------------------------------------------------------- agent
variable "agent_image_tag" {
  description = "Tag pushed to the agent repository of terraform/base/ecr (step 2 of ops/up.sh). The repository URL is read from terraform/base/ecr/terraform.tfstate."
  type        = string
  default     = "v1"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.agent_image_tag))
    error_message = "agent_image_tag must match ^[A-Za-z0-9._-]{1,128}$."
  }
}

variable "agent_image_uri" {
  description = "Optional. Leave empty to use <terraform/base/ecr repository>:<agent_image_tag>. Set only to run an image from another repository, with tag, built for linux/arm64 (e.g. 123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/<owner>-nwc-poc-agent:v1)."
  type        = string
  default     = ""
}

variable "runtime_name" {
  description = "Optional. AgentCore Runtime name (letters, digits and underscore only. Hyphens are not allowed). Leave empty to derive it from the prefix <owner>-nwc-poc: hyphens become underscores and _agent is appended (owner netops -> netops_nwc_poc_agent)."
  type        = string
  default     = ""

  validation {
    # 空なら locals.tf が接頭辞から作る（接頭辞は ^[a-z][a-z0-9-]{1,22}$ なので必ずこの形に収まる）
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
