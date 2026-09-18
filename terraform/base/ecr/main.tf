# ECR repositories of netops-poc. The agent image (docs/deploy-manual.md step 2), the three lab images (docs/pipeline.md lab-1)
# and the two workflow images (worker / temporal, docs/workflow.md w-1) go here.
# force_delete = true so that `terraform destroy` removes the repositories together with their images (daily ops/down.sh).

locals {
  lab_repositories      = var.create_lab_repositories ? toset(["frr", "snmpd", "multitool"]) : toset([])
  workflow_repositories = var.create_workflow_repositories ? toset(["worker", "temporal"]) : toset([])
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

resource "aws_ecr_repository" "workflow" {
  for_each = local.workflow_repositories

  name                 = "${var.name_prefix}-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}
