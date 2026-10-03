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

# ---------------------------------------------------------------- nautobot (ECS)
variable "nautobot_image_tag" {
  description = "Tag of the Nautobot image in the ECR repository <prefix>-nautobot. ops/up.sh builds nautobot/ and passes <Nautobot version>-<hash of the build context>."
  type        = string
  default     = "3.2.6"
}

variable "redis_image_tag" {
  description = "Tag of the Redis image in the ECR repository <prefix>-redis (mirrored by ops/up.sh, REDIS_TAG)."
  type        = string
  default     = "7.4.2-alpine"
}

variable "task_cpu" {
  description = "CPU units of the task (web + Celery worker + Redis). The database migration of the first start is the heavy part."
  type        = number
  default     = 2048
}

variable "task_memory" {
  description = "Memory (MiB) of the task"
  type        = number
  default     = 4096
}

variable "admin_user" {
  description = "Name of the Nautobot superuser that the task creates at start (the password is the SSM parameter /<prefix>/nautobot/admin-password)."
  type        = string
  default     = "admin"
}

variable "log_retention_days" {
  description = "Retention of the log group /ecs/<prefix>-nautobot."
  type        = number
  default     = 7

  validation {
    condition     = contains([1, 3, 7, 14, 30], var.log_retention_days)
    error_message = "log_retention_days must be one of 1, 3, 7, 14, 30."
  }
}

# ---------------------------------------------------------------- database (RDS)
variable "db_instance_class" {
  description = "RDS instance class of the Nautobot database (about 0.03 USD per hour for db.t4g.micro in Tokyo - list price, not verified with the pricing API)."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_engine_version" {
  description = "PostgreSQL major version (RDS picks the minor). Nautobot 3 supports PostgreSQL 12.0 and later."
  type        = string
  default     = "17"
}

variable "db_password_version" {
  description = "Raise by one to make Terraform send the password of /<prefix>/nautobot/db-password to RDS again (the password is write-only, so Terraform cannot see that the parameter changed)."
  type        = number
  default     = 1
}

variable "deletion_protection" {
  description = "Keep false so terraform destroy can delete the database the same day."
  type        = bool
  default     = false
}
