provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = local.name_prefix
      owner   = var.owner
    }
  }
}
