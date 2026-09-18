# ---------------------------------------------------------------- naming
variable "region" {
  description = "AWS region. Same value as terraform/base/core."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Same value as terraform/base/ecr / terraform/base/core (ECR repository names are derived from it)."
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
  description = "Value of the owner tag."
  type        = string
  default     = "netops"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.owner))
    error_message = "owner must match ^[A-Za-z0-9._-]{1,64}$."
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
  description = "telegraf-<version>-1.aarch64.rpm in s3://<kb_bucket_name of terraform/base/core>/lab/ is installed when present (docs/pipeline.md s-1). Nothing happens without it."
  type        = string
  default     = "1.40.0"

  validation {
    condition     = can(regex("^[0-9]+[.][0-9]+[.][0-9]+$", var.telegraf_version))
    error_message = "telegraf_version must look like 1.40.0."
  }
}

variable "frr_image_tag" {
  description = "Tag pushed to <name_prefix>-lab-frr"
  type        = string
  default     = "10.2.1"
}

variable "snmpd_image_tag" {
  description = "Tag pushed to <name_prefix>-lab-snmpd"
  type        = string
  default     = "v1"
}

variable "multitool_image_tag" {
  description = "Tag pushed to <name_prefix>-lab-multitool"
  type        = string
  default     = "v0.10.0"
}
