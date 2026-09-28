# ECR repositories of netops-poc. The agent image, the two lab images (srlinux / multitool),
# the two workflow images (worker / temporal) and the three ECS images of the pipeline (telegraf / grafana / splunk) go here.
# ops/up.sh pushes them in step 2.
# force_delete = true so that `terraform destroy` removes the repositories together with their images (daily ops/down.sh).

# リソース名の接頭辞であり Project タグの値。デプロイする人の名前（var.owner）から作るので、
# 1 つの AWS アカウントを何人かで使っても、自分の名前で自分のリソースを探せる
locals {
  name_prefix = "${var.owner}-nwc-poc"
}

locals {
  lab_repositories      = var.create_lab_repositories ? toset(["srlinux", "multitool"]) : toset([])
  workflow_repositories = var.create_workflow_repositories ? toset(["worker", "temporal"]) : toset([])
  # ECS で動かすパイプラインの 3 つ（Telegraf は pipeline/stream、Grafana と Splunk は pipeline/analytics）。リポジトリに時間課金は無いので、いつも作る
  pipeline_repositories = toset(["telegraf", "grafana", "splunk"])
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

resource "aws_ecr_repository" "pipeline" {
  for_each = local.pipeline_repositories

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
