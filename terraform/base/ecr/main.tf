# ECR repositories of netops-poc. The agent image, the three lab images
# and the two workflow images (worker / temporal) go here. ops/up.sh pushes them in step 2.
# force_delete = true so that `terraform destroy` removes the repositories together with their images (daily ops/down.sh).

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

locals {
  lab_repositories      = var.create_lab_repositories ? toset(["frr", "snmpd", "multitool"]) : toset([])
  workflow_repositories = var.create_workflow_repositories ? toset(["worker", "temporal"]) : toset([])
}

resource "aws_ecr_repository" "agent" {
  name                 = "${local.name_prefix}-agent"
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

  name                 = "${local.name_prefix}-lab-${each.key}"
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

  name                 = "${local.name_prefix}-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}
