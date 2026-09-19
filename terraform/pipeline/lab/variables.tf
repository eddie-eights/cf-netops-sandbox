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

# ---------------------------------------------------------------- lab EC2
variable "instance_type" {
  description = "14 containers (FRR x6, snmpd x4, hosts x4). t4g.large (2 vCPU / 8 GB) is comfortable; t4g.medium (4 GB) works but leaves little room."
  type        = string
  default     = "t4g.large"

  validation {
    condition     = contains(["t4g.medium", "t4g.large", "t4g.xlarge"], var.instance_type)
    error_message = "instance_type must be t4g.medium, t4g.large or t4g.xlarge (arm64)."
  }
}

variable "ami_ssm_parameter" {
  description = "SSM public parameter that resolves to the AMI. Amazon Linux 2023 arm64 (SSM Agent and AWS CLI v2 are preinstalled; Docker comes from the AL2023 repository)."
  type        = string
  default     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

variable "volume_size" {
  description = "Root volume in GB (Docker images are about 400 MB)."
  type        = number
  default     = 16

  validation {
    condition     = var.volume_size >= 8 && var.volume_size <= 64
    error_message = "volume_size must be between 8 and 64."
  }
}

variable "auto_start_lab" {
  description = "Deploy the topology at every boot (systemd unit). Set false to start it by hand with \"sudo lab up\"."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- Telegraf EC2 (telegraf.tf)
variable "create_telegraf" {
  description = "Create the Telegraf EC2 that sends SNMP polls, traps and FRR logs of the lab to MSK (terraform/pipeline/stream). ops/up.sh sets true when it makes the stream root. terraform/pipeline/stream needs it."
  type        = bool
  default     = false
}

variable "telegraf_instance_type" {
  description = "Telegraf only (no containers). t4g.micro (1 GB) is enough for 4 SNMP agents, traps and the FRR logs."
  type        = string
  default     = "t4g.micro"

  validation {
    condition     = contains(["t4g.nano", "t4g.micro", "t4g.small", "t4g.medium"], var.telegraf_instance_type)
    error_message = "telegraf_instance_type must be t4g.nano, t4g.micro, t4g.small or t4g.medium (arm64)."
  }
}

# ---------------------------------------------------------------- assets
variable "containerlab_version" {
  description = "containerlab_<version>_linux_arm64.rpm must be uploaded to s3://<kb_bucket_name of terraform/base/core>/lab/"
  type        = string
  default     = "0.79.0"

  validation {
    condition     = can(regex("^[0-9]+[.][0-9]+[.][0-9]+$", var.containerlab_version))
    error_message = "containerlab_version must look like 0.79.0."
  }
}

variable "telegraf_version" {
  description = "telegraf-<version>-1.aarch64.rpm must be uploaded to s3://<kb_bucket_name of terraform/base/core>/telegraf/ with telegraf/ of this repository (step 5 of ops/up.sh). The Telegraf EC2 does nothing without it."
  type        = string
  default     = "1.40.0"

  validation {
    condition     = can(regex("^[0-9]+[.][0-9]+[.][0-9]+$", var.telegraf_version))
    error_message = "telegraf_version must look like 1.40.0."
  }
}

variable "frr_image_tag" {
  description = "Tag pushed to <prefix>-lab-frr"
  type        = string
  default     = "10.2.1"
}

variable "snmpd_image_tag" {
  description = "Tag pushed to <prefix>-lab-snmpd"
  type        = string
  default     = "v1"
}

variable "multitool_image_tag" {
  description = "Tag pushed to <prefix>-lab-multitool"
  type        = string
  default     = "v0.10.0"
}
