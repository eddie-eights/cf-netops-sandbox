terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    # KB のベクトル索引を作る Lambda（kb.tf の kb_index）の zip。create_knowledge_base = false でも zip だけは作る
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
  }
}
