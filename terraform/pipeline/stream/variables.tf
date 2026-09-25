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
  description = "Smallest Standard broker that Kafka 4.x (KRaft) accepts. kafka.t3.small is rejected by CreateCluster with 4.1.x.kraft (Unsupported InstanceType, seen 2026-09-18); it is only for 3.x. kafka.m5.large is 0.271 USD per hour per broker in Tokyo, so 0.542 for the 2 brokers (Price List API, 2026-09-18)."
  type        = string
  default     = "kafka.m5.large"

  validation {
    condition     = contains(["kafka.m5.large", "kafka.m7g.large"], var.broker_instance_type)
    error_message = "broker_instance_type must be kafka.m5.large or kafka.m7g.large (Kafka 4.x does not accept kafka.t3.small)."
  }
}

variable "log_retention_days" {
  description = "Retention of the broker log group."
  type        = number
  default     = 7

  validation {
    condition     = contains([1, 3, 7, 14, 30], var.log_retention_days)
    error_message = "log_retention_days must be one of 1, 3, 7, 14, 30."
  }
}
