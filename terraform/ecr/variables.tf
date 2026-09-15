variable "region" {
  description = "AWS region. Every root module of this repository uses the same value."
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "Prefix of every resource name (<name_prefix>-agent, <name_prefix>-lab-frr, ...). Same value in every root module."
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

variable "create_lab_repositories" {
  description = "Also create the three repositories of the lab images (frr / snmpd / multitool, README lab-1). false keeps only the agent repository."
  type        = bool
  default     = true
}
