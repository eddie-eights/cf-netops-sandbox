terraform {
  required_version = ">= 1.11.0, < 2.0.0" # 1.11: write-only arguments (password_wo of the RDS instance) and ephemeral resources

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
