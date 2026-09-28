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
  description = "8 containers (Nokia SR Linux x6, VMs x2). Each SR Linux node takes about 1.5-2.5 GB of RAM at boot, so t4g.xlarge (4 vCPU / 16 GB) is the default; t4g.large (8 GB) is too small for six nodes."
  type        = string
  default     = "t4g.xlarge"

  validation {
    condition     = contains(["t4g.large", "t4g.xlarge", "t4g.2xlarge"], var.instance_type)
    error_message = "instance_type must be t4g.large, t4g.xlarge or t4g.2xlarge (arm64)."
  }
}

variable "ami_ssm_parameter" {
  description = "SSM public parameter that resolves to the AMI. Amazon Linux 2023 arm64 (SSM Agent and AWS CLI v2 are preinstalled; Docker comes from the AL2023 repository)."
  type        = string
  default     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

variable "volume_size" {
  description = "Root volume in GB (the SR Linux image is about 1 GB; each node writes its own logs and checkpoints)."
  type        = number
  default     = 24

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

# ---------------------------------------------------------------- path to Telegraf (telegraf.tf)
variable "forward_to_telegraf" {
  description = "Route the lab mgmt network (203.0.113.0/24) to this EC2 and open the security group for the traps and syslog, so Telegraf (the ECS task of terraform/pipeline/stream) reaches the switches. ops/up.sh sets true when terraform/pipeline/stream is made or kept."
  type        = bool
  default     = false
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

variable "srlinux_image_tag" {
  description = "Tag pushed to <prefix>-lab-srlinux (ghcr.io/nokia/srlinux:<tag>, multi-arch; ops/up.sh pulls the arm64 image)."
  type        = string
  default     = "26.7.2"
}

variable "multitool_image_tag" {
  description = "Tag pushed to <prefix>-lab-multitool"
  type        = string
  default     = "v0.10.0"
}
