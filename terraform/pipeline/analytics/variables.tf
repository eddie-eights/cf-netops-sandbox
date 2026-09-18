variable "region" {
  description = "Region. terraform/base/core and terraform/pipeline/stream must be in the same region"
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Prefix of every resource name. Must match terraform/base/core and terraform/pipeline/stream"
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
  description = "Value of the owner tag on every resource"
  type        = string
  default     = "netops"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.owner))
    error_message = "owner must match ^[A-Za-z0-9._-]{1,64}$."
  }
}

variable "emr_release_label" {
  description = "EMR Serverless release. 7.13.0 = Spark 3.5.6 (checked 2026-09-17). The Kafka jars that ops/up.sh uploads are pinned to this Spark version - change both together"
  type        = string
  default     = "emr-7.13.0"

  validation {
    condition     = can(regex("^emr-7\\.(5|[6-9]|1[0-9])\\.[0-9]+$", var.emr_release_label))
    error_message = "S3 Tables は EMR 7.5.0 以上（AWS ドキュメント、2026-09-17 確認）。emr-7.5.0〜emr-7.19.x の形で書く。"
  }
}

variable "namespace" {
  description = "S3 Tables namespace (lowercase letters, digits, underscores - no hyphens)"
  type        = string
  default     = "netops"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9_]{0,254}$", var.namespace)) && !startswith(var.namespace, "aws")
    error_message = "namespace は小文字英数字とアンダースコア、1〜255 文字、aws で始めない（S3 Tables の命名規則。ハイフンは使えない）。"
  }
}

variable "table_name" {
  description = "S3 Tables table that the Spark job appends the Telegraf messages to (lowercase letters, digits, underscores)"
  type        = string
  default     = "snmp_metrics"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9_]{0,254}$", var.table_name))
    error_message = "table_name は小文字英数字とアンダースコア、1〜255 文字（S3 Tables の命名規則。ハイフンは使えない）。"
  }
}

variable "max_cpu" {
  description = "Upper bound of vCPU the application may use at once (EMR Serverless maximumCapacity). The streaming job asks for 2 (driver 1 + executor 1)"
  type        = string
  default     = "4 vCPU"
}

variable "max_memory" {
  description = "Upper bound of memory the application may use at once"
  type        = string
  default     = "16 GB"
}

variable "idle_timeout_minutes" {
  description = "Minutes without a running job before the application stops itself (it restarts on the next start-job-run)"
  type        = number
  default     = 15
}

variable "log_retention_days" {
  description = "Retention of the CloudWatch log group the job driver writes to"
  type        = number
  default     = 7
}

variable "cloudwatch_logging" {
  description = "Send the driver stdout / stderr to CloudWatch Logs. Needs the logs interface endpoint of the base root (create_shared_endpoints, default true). false keeps the logs only in the asset bucket"
  type        = bool
  default     = true
}

variable "sinks" {
  description = "Where the Spark job stores the Telegraf messages: iceberg (all topics to S3 Tables, tables.tf), opensearch (log topics to an OpenSearch Serverless TIMESERIES collection made here), prometheus (metric topics to an Amazon Managed Service for Prometheus workspace made here). One streaming query per entry. Splunk is not a Spark sink (it will be an MSK Connect connector in terraform/pipeline/stream, not built yet)"
  type        = list(string)
  default     = ["iceberg", "opensearch", "prometheus"]

  validation {
    condition     = length(var.sinks) > 0 && length(setsubtract(var.sinks, ["iceberg", "opensearch", "prometheus"])) == 0
    error_message = "sinks は iceberg / opensearch / prometheus のリスト（1 つ以上）。"
  }
}

# ---------------------------------------------------------------- detection (Spark -> DynamoDB -> EventBridge)
variable "device_map" {
  description = "ip=device_id,... used by the detection query when a message has no sysName tag (traps). Matches lab/wanlab.clab.yml.in and agent/data/devices.yaml."
  type        = string
  default     = "203.0.113.11=hq-ce-01,203.0.113.12=dc-ce-01,203.0.113.13=br1-ce-01,203.0.113.14=br2-ce-01"
}

variable "event_bus" {
  description = "EventBridge event bus the job puts AnomalyOpened events on. terraform/workflow subscribes to the same bus."
  type        = string
  default     = "default"
}

variable "metric_topics" {
  description = "Kafka topics that carry metrics (Telegraf inputs.snmp). Read by the iceberg and prometheus sinks"
  type        = list(string)
  default     = ["metrics"]

  validation {
    condition     = length(var.metric_topics) > 0
    error_message = "metric_topics は 1 つ以上。"
  }
}

variable "log_topics" {
  description = "Kafka topics that carry logs (traps = Telegraf inputs.snmp_trap, logs = the FRR log files Telegraf tails on the lab EC2). Read by the iceberg and opensearch sinks"
  type        = list(string)
  default     = ["traps", "logs"]

  validation {
    condition     = length(var.log_topics) > 0
    error_message = "log_topics は 1 つ以上。"
  }
}
