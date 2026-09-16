variable "region" {
  description = "Region. terraform/main and terraform/stream must be in the same region"
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Prefix of every resource name. Must match terraform/main and terraform/stream"
  type        = string
  default     = "fukuda-nwc-poc"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,24}$", var.name_prefix))
    error_message = "name_prefix は小文字英数字とハイフン、先頭は英字、2〜25 文字（S3 Tables のテーブルバケット名 <prefix>-tables が 63 文字以内に収まるように）。"
  }
}

variable "owner" {
  description = "Value of the owner tag on every resource"
  type        = string
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

variable "sinks" {
  description = "Where the Spark job stores the Telegraf messages: iceberg (all topics to S3 Tables, tables.tf), opensearch (log topics to an OpenSearch Serverless TIMESERIES collection made here), prometheus (metric topics to an Amazon Managed Service for Prometheus workspace made here). One streaming query per entry. Splunk is not a Spark sink (it will be an MSK Connect connector in terraform/stream, not built yet)"
  type        = list(string)
  default     = ["iceberg"]

  validation {
    condition     = length(var.sinks) > 0 && length(setsubtract(var.sinks, ["iceberg", "opensearch", "prometheus"])) == 0
    error_message = "sinks は iceberg / opensearch / prometheus のリスト（1 つ以上）。"
  }
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
  description = "Kafka topics that carry logs (Telegraf inputs.snmp_trap; add logs once the lab Telegraf has a syslog input). Read by the iceberg and opensearch sinks"
  type        = list(string)
  default     = ["traps"]

  validation {
    condition     = length(var.log_topics) > 0
    error_message = "log_topics は 1 つ以上。"
  }
}
