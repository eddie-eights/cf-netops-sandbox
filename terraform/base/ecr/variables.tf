variable "region" {
  description = "AWS region. Every root module of this repository uses the same value."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Prefix of every resource name (<name_prefix>-agent, <name_prefix>-lab-frr, ...). Same value in every root module."
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
  description = "Value of the owner tag."
  type        = string
  default     = "netops"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.owner))
    error_message = "owner must match ^[A-Za-z0-9._-]{1,64}$."
  }
}

variable "create_lab_repositories" {
  description = "Also create the three repositories of the lab images (frr / snmpd / multitool, docs/pipeline.md lab-1). false keeps only the agent repository."
  type        = bool
  default     = true
}

variable "create_workflow_repositories" {
  description = "Also create the two repositories of the WORKFLOW images (worker / temporal). false keeps only the agent and lab repositories."
  type        = bool
  default     = true
}
