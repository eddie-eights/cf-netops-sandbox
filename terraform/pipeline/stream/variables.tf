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

# ---------------------------------------------------------------- Telegraf (telegraf.tf)
variable "telegraf_image_tag" {
  description = "Tag of the Telegraf image in the <prefix>-telegraf repository (telegraf/Dockerfile). ops/up.sh builds it as <telegraf version>-<hash of telegraf/> and passes it."
  type        = string
  default     = "1.40.0"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.telegraf_image_tag))
    error_message = "telegraf_image_tag must be a valid ECR tag (letters, digits, . _ -)."
  }
}

variable "snmp_agents" {
  description = "SNMP polling targets of the Telegraf dial-in task, as the inside of a TOML list (\"udp://<IP>:161\", ...). ops/up.sh makes it from the lab definition (python3 lab/lab_topology.py lab --snmp-agents). Goes to the SSM parameter .../telegraf-dialin/lab/snmp-agents (or only the first value of .../nautobot/snmp-agents with dialin_targets_from_nautobot)."
  type        = string

  validation {
    condition     = can(regex("^\"udp://[0-9.]+:[0-9]+\"(, *\"udp://[0-9.]+:[0-9]+\")*$", var.snmp_agents))
    error_message = "snmp_agents must look like \"udp://203.0.113.11:161\", \"udp://203.0.113.12:161\" (python3 lab/lab_topology.py lab --snmp-agents)."
  }
}

variable "gnmi_targets" {
  description = "gNMI subscription targets of the Telegraf dial-in task, as the inside of a TOML list (\"<IP>:57400\", ...). ops/up.sh makes it from the lab definition (python3 lab/lab_topology.py lab --gnmi-targets). Goes to the SSM parameter .../telegraf-dialin/lab/gnmi-targets (or only the first value of .../nautobot/gnmi-targets with dialin_targets_from_nautobot)."
  type        = string

  validation {
    condition     = can(regex("^\"[0-9.]+:[0-9]+\"(, *\"[0-9.]+:[0-9]+\")*$", var.gnmi_targets))
    error_message = "gnmi_targets must look like \"203.0.113.11:57400\", \"203.0.113.12:57400\" (python3 lab/lab_topology.py lab --gnmi-targets)."
  }
}

variable "dialin_targets_from_nautobot" {
  description = "Whether the Nautobot job (terraform/pipeline/nautobot) owns the dial-in targets. true moves them to /<prefix>/telegraf-dialin/nautobot/* (Terraform writes only the first value, from snmp_agents / gnmi_targets, and ignores later changes); false keeps them in /<prefix>/telegraf-dialin/lab/* from the variables. ops/up.sh sets it from NAUTOBOT."
  type        = bool
  default     = false
}

variable "syslog_standard" {
  description = "Format of the device syslog that Telegraf parses (inputs.syslog syslog_standard). RFC3164 is the BSD format of Cisco IOS, the production devices. ops/up.sh passes SYSLOG_STANDARD from deploy.env (default RFC3164). The SR Linux lab sends RFC5424 (LAB_SYSLOG_STANDARD in ops/lab-common.sh)."
  type        = string
  default     = "RFC3164"

  validation {
    condition     = contains(["RFC3164", "RFC5424"], var.syslog_standard)
    error_message = "syslog_standard must be RFC3164 or RFC5424."
  }
}

variable "snmp_poll" {
  description = "Whether Telegraf polls SNMP (inputs.snmp, ifTable every 10 seconds) and writes it to the metrics topic. Off by default: SNMP comes in as traps only, so the Grafana rule link_down (it reads the polled ifOperStatus) stays silent and link down is seen by the Splunk saved search on traps (SINK_SPLUNK=1). ops/up.sh passes SNMP_POLL from deploy.env. Becomes SNMP_POLL (1 / 0) of the task."
  type        = bool
  default     = false
}

variable "telegraf_task_cpu" {
  description = "Fargate CPU units of the Telegraf task (ARM64). 256 (0.25 vCPU) is enough for 6 SNMP agents, 6 gNMI subscriptions, traps and syslog."
  type        = number
  default     = 256

  validation {
    condition     = contains([256, 512, 1024], var.telegraf_task_cpu)
    error_message = "telegraf_task_cpu must be 256, 512 or 1024."
  }
}

variable "telegraf_task_memory" {
  description = "Fargate memory (MiB) of the Telegraf task. Must be a valid pair with telegraf_task_cpu (256 takes 512-2048)."
  type        = number
  default     = 512

  validation {
    condition     = contains([512, 1024, 2048], var.telegraf_task_memory)
    error_message = "telegraf_task_memory must be 512, 1024 or 2048."
  }
}
