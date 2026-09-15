# ECR repositories of fukuda-nwc-poc. The agent image (README step 2) and the three lab images (README lab-1) go here.
# force_delete = true so that `terraform destroy` removes the repositories together with their images (daily ops/down.sh).

locals {
  lab_repositories = var.create_lab_repositories ? toset(["frr", "snmpd", "multitool"]) : toset([])
}

resource "aws_ecr_repository" "agent" {
  name                 = "${var.name_prefix}-agent"
  image_tag_mutability = "IMMUTABLE" # the same tag cannot be pushed twice (change agent_image_tag / IMAGE_TAG for every update)
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "agent" {
  repository = aws_ecr_repository.agent.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_repository" "lab" {
  for_each = local.lab_repositories

  name                 = "${var.name_prefix}-lab-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}
