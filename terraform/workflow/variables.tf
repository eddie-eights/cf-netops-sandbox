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

# ---------------------------------------------------------------- images (terraform/base/ecr)
variable "worker_image_tag" {
  description = "Tag of the worker image in the <prefix>-worker repository. ops/up.sh passes IMAGE_TAG."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.worker_image_tag))
    error_message = "worker_image_tag must be a valid ECR tag (letters, digits, . _ -)."
  }
}

variable "temporal_image_tag" {
  description = "Tag of the Temporal CLI image mirrored into the <prefix>-temporal repository (temporalio/temporal)."
  type        = string
  default     = "1.9.1"
}

# ---------------------------------------------------------------- task
variable "task_cpu" {
  description = "Fargate task CPU units (1024 = 1 vCPU). Temporal dev server and the worker share it."
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "Fargate task memory in MiB."
  type        = number
  default     = 2048
}

variable "desired_count" {
  description = "Number of workflow tasks. Keep 1 - the Temporal dev server stores its state in the task (SQLite) and two tasks would not share it."
  type        = number
  default     = 1

  validation {
    condition     = var.desired_count >= 0 && var.desired_count <= 1
    error_message = "desired_count must be 0 or 1."
  }
}

variable "log_retention_days" {
  description = "Retention of the task log group /ecs/<prefix>-workflow."
  type        = number
  default     = 7

  validation {
    condition     = contains([1, 3, 7, 14, 30], var.log_retention_days)
    error_message = "log_retention_days must be one of 1, 3, 7, 14, 30."
  }
}

# ---------------------------------------------------------------- workflow behaviour (worker environment)
variable "poll_interval_seconds" {
  description = "How often the worker lists the open anomalies in Neptune when it has no SQS queue (ANOMALY_QUEUE_URL empty). With the queue (default) the worker long-polls SQS instead and this is only the retry interval after an error."
  type        = number
  default     = 60
}

variable "approval_timeout_minutes" {
  description = "How long a proposal waits for a human decision before the workflow ends as expired."
  type        = number
  default     = 120
}

variable "verify_attempts" {
  description = "How many times the workflow re-reads the anomaly after applying the fix (30 seconds apart) before giving up."
  type        = number
  default     = 6
}

# ---------------------------------------------------------------- gateway (MCP)
variable "create_gateway" {
  description = "Create the AgentCore Gateway (MCP) with the tools Lambda (in the VPC: Neptune, OpenSearch Serverless, Prometheus). false keeps the workflow only; the chat runtime then uses its built-in tools."
  type        = bool
  default     = true
}
