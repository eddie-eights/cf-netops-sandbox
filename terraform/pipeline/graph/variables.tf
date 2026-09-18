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

# ---------------------------------------------------------------- neptune
variable "instance_class" {
  description = "db.t4g.medium is the smallest Neptune class (about 0.11 USD per hour in Tokyo - price not yet verified with the pricing API)."
  type        = string
  default     = "db.t4g.medium"

  validation {
    condition     = contains(["db.t4g.medium", "db.r6g.large"], var.instance_class)
    error_message = "instance_class must be db.t4g.medium or db.r6g.large."
  }
}

variable "engine_version" {
  description = "Neptune engine version (1.4.8.0 checked 2026-09-15). Fails at apply time if the region does not offer it."
  type        = string
  default     = "1.4.8.0"

  validation {
    condition     = can(regex("^[0-9]+([.][0-9]+){2,3}$", var.engine_version))
    error_message = "engine_version must look like 1.4.8.0."
  }
}

variable "deletion_protection" {
  description = "Keep false so terraform destroy can delete the cluster the same day."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------- status Lambda
variable "log_retention_days" {
  description = "Retention of the status Lambda log group /aws/lambda/<prefix>-graph-status."
  type        = number
  default     = 7

  validation {
    condition     = contains([1, 3, 7, 14, 30], var.log_retention_days)
    error_message = "log_retention_days must be one of 1, 3, 7, 14, 30."
  }
}
