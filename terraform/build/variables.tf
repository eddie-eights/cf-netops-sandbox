variable "region" {
  description = "AWS region. Same value as terraform/ecr."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Same value as terraform/ecr. The project pushes to <name_prefix>-agent and <name_prefix>-lab-*."
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

variable "source_object_key" {
  description = "Object key of the zip in the source bucket that this root module creates. The zip holds agent/ and lab/snmpd/ (README step 2-b)."
  type        = string
  default     = "src.zip"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]{1,200}\\.zip$", var.source_object_key))
    error_message = "source_object_key must be a .zip key (letters, digits, . _ / -)."
  }
}

variable "log_retention_days" {
  description = "Retention of the CodeBuild log group."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90], var.log_retention_days)
    error_message = "log_retention_days must be one of 1, 3, 5, 7, 14, 30, 60, 90."
  }
}
