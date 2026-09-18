# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/base/core."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Same value as terraform/base/core (the IAM roles <name_prefix>-runtime / <name_prefix>-web get the Neptune policy)."
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
  description = "Retention of the status Lambda log group /aws/lambda/<name_prefix>-graph-status."
  type        = number
  default     = 7

  validation {
    condition     = contains([1, 3, 7, 14, 30], var.log_retention_days)
    error_message = "log_retention_days must be one of 1, 3, 7, 14, 30."
  }
}
