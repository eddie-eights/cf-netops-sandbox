provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = var.name_prefix
      owner   = var.owner
    }
  }
}
