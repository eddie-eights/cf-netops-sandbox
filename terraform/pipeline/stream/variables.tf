# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/base/core."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Same value as terraform/base/core (the IAM roles <name_prefix>-runtime / <name_prefix>-web get the DynamoDB policy)."
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

# ---------------------------------------------------------------- network (same VPC as terraform/base/core)
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
  description = "MSK provisioned Kafka version, KRaft mode only (the .kraft suffix selects KRaft; Kafka 4 has no ZooKeeper mode). 4.1.x is the newest for Standard brokers, 4.2.x is Express brokers only (list-kafka-versions and the MSK supported versions page, checked 2026-09-18)."
  type        = string
  default     = "4.1.x.kraft"

  validation {
    condition     = can(regex("^([4-9]|[1-9][0-9])\\.[0-9]+\\.x\\.kraft$", var.kafka_version))
    error_message = "kafka_version must be 4.0.x.kraft or newer (KRaft mode), e.g. 4.1.x.kraft."
  }
}

variable "broker_instance_type" {
  description = "Smallest Standard broker that Kafka 4.x (KRaft) accepts. kafka.t3.small is rejected by CreateCluster with 4.1.x.kraft (Unsupported InstanceType, seen 2026-09-18); it is only for 3.x. kafka.m7g.large is 0.2635 USD per hour per broker in Tokyo, so 0.527 for the 2 brokers (Price List API, 2026-09-18)."
  type        = string
  default     = "kafka.m7g.large"

  validation {
    condition     = contains(["kafka.m7g.large", "kafka.m5.large"], var.broker_instance_type)
    error_message = "broker_instance_type must be kafka.m7g.large or kafka.m5.large (Kafka 4.x does not accept kafka.t3.small)."
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
  description = "Key of the Confluent S3 sink connector zip in the asset bucket of terraform/base/core (download from Confluent Hub and upload before apply, README s-1)."
  type        = string
  default     = "stream/confluentinc-kafka-connect-s3-12.1.11.zip"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+[.]zip$", var.s3_sink_plugin_key))
    error_message = "s3_sink_plugin_key must be an S3 key that ends with .zip."
  }
}
