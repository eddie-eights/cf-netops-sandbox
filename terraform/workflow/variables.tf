# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/base/core."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Same value as terraform/base/core (the IAM roles <name_prefix>-runtime / <name_prefix>-web get the proposal table and gateway policy)."
  type        = string
  default     = "netops-poc"

  validation {
    # 一番きついのは OpenSearch Serverless の data access policy 名（32 文字まで）で、一番長い接尾辞はこのルートの <name_prefix>-logs-read（10 文字）。だから 22 文字に抑える
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

# ---------------------------------------------------------------- images (terraform/base/ecr)
variable "worker_image_tag" {
  description = "Tag of the worker image in the <name_prefix>-worker repository (docs/workflow.md w-1). ops/up.sh passes IMAGE_TAG."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.worker_image_tag))
    error_message = "worker_image_tag must be a valid ECR tag (letters, digits, . _ -)."
  }
}

variable "temporal_image_tag" {
  description = "Tag of the Temporal CLI image mirrored into the <name_prefix>-temporal repository (temporalio/temporal, docs/workflow.md w-1)."
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
  description = "Retention of the task log group /ecs/<name_prefix>-workflow."
  type        = number
  default     = 7

  validation {
    condition     = contains([1, 3, 7, 14, 30], var.log_retention_days)
    error_message = "log_retention_days must be one of 1, 3, 7, 14, 30."
  }
}

# ---------------------------------------------------------------- workflow behaviour (worker environment)
variable "poll_interval_seconds" {
  description = "How often the worker scans the anomaly table for open anomalies when it has no SQS queue (ANOMALY_QUEUE_URL empty). With the queue (default) the worker long-polls SQS instead and this is only the retry interval after an error."
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

variable "create_sqs_endpoint" {
  description = "sqs interface endpoint (1 AZ, the task subnet) so the worker can receive the AnomalyOpened messages without a NAT. Set false if the VPC already has one."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- gateway (MCP)
variable "create_gateway" {
  description = "Create the AgentCore Gateway (MCP) with the tools Lambda (in the VPC: Neptune, OpenSearch Serverless, Prometheus, anomaly table). false keeps the workflow only; the chat runtime then uses its built-in tools."
  type        = bool
  default     = true
}
