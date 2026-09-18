variable "region" {
  description = "AWS region. Every root module of this repository uses the same value."
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
