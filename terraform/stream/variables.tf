# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/main."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Same value as terraform/main (the IAM roles <name_prefix>-runtime / <name_prefix>-web get the DynamoDB policy)."
  type        = string
  default     = "fukuda-nwc-poc"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,24}$", var.name_prefix))
    error_message = "name_prefix must match ^[a-z][a-z0-9-]{1,24}$."
  }
}

variable "owner" {
  description = "Value of the owner tag."
  type        = string
  default     = "fukuda"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.owner))
    error_message = "owner must match ^[A-Za-z0-9._-]{1,64}$."
  }
}

# ---------------------------------------------------------------- network (same VPC as terraform/main)
variable "create_sts_endpoint" {
  description = "sts interface endpoint (1 AZ, first subnet) for the MSK Connect workers, because the VPC has no NAT. Whether MSK Connect really needs it has not been verified (2026-09-17). Set false if the VPC already has one."
  type        = bool
  default     = true
}

variable "create_dynamodb_endpoint" {
  description = "DynamoDB gateway endpoint (free) so the chat web EC2 reads the anomaly table without NAT. Set false if the VPC already has one."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- MSK
variable "kafka_version" {
  description = "MSK provisioned Kafka version. 3.9.x is the recommended version (checked 2026-09-15)."
  type        = string
  default     = "3.9.x"
}

variable "broker_instance_type" {
  description = "kafka.t3.small is the smallest provisioned broker (2 brokers, about 0.09 USD per hour together in Tokyo - verify in the pricing page)."
  type        = string
  default     = "kafka.t3.small"

  validation {
    condition     = contains(["kafka.t3.small", "kafka.m7g.large"], var.broker_instance_type)
    error_message = "broker_instance_type must be kafka.t3.small or kafka.m7g.large."
  }
}

variable "log_retention_days" {
  description = "Retention of the broker and MSK Connect log groups."
  type        = number
  default     = 7

  validation {
    condition     = contains([1, 3, 7, 14, 30], var.log_retention_days)
    error_message = "log_retention_days must be one of 1, 3, 7, 14, 30."
  }
}

# ---------------------------------------------------------------- MSK Connect S3 sink
variable "create_s3_sink" {
  description = "MSK Connect S3 sink (1 MCU, about 0.11 USD per hour). Needs the plugin zip at s3_sink_plugin_key before apply (README s-1). Set false to run without the sink."
  type        = bool
  default     = true
}

variable "kafka_connect_version" {
  description = "Kafka Connect version of MSK Connect. The allowed values are not listed in the provider documentation (not verified) - 2.7.1 is the value shown in the MSK Connect documentation examples."
  type        = string
  default     = "2.7.1"
}

variable "s3_sink_plugin_key" {
  description = "Key of the Confluent S3 sink connector zip in the asset bucket of terraform/main (download from Confluent Hub and upload before apply, README s-1)."
  type        = string
  default     = "stream/confluentinc-kafka-connect-s3-12.1.11.zip"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+[.]zip$", var.s3_sink_plugin_key))
    error_message = "s3_sink_plugin_key must be an S3 key that ends with .zip."
  }
}
