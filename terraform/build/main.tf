# fukuda-nwc-poc - optional CodeBuild project that builds the arm64 images in AWS (native arm64, no QEMU, no corporate
# proxy or certificate in the way) from a zip you upload to the source bucket of this root module, and pushes them to the
# repositories of terraform/ecr. Replaces README step 2 and lab-1 when the PC cannot build. Costs nothing while idle.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
}

# ---------------------------------------------------------------- logs
resource "aws_cloudwatch_log_group" "build" {
  name              = "/aws/codebuild/${var.name_prefix}-build"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------- source bucket
# ここに agent/ と lab/snmpd/ を zip で置く（コンソールのアップロードでよい）。7 日で消えるので放置しても溜まらない。
# force_destroy = true なので中身が残っていても terraform destroy で消える
resource "aws_s3_bucket" "source" {
  bucket        = "${var.name_prefix}-build-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "source" {
  bucket                  = aws_s3_bucket.source.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "source" {
  bucket = aws_s3_bucket.source.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "source" {
  bucket = aws_s3_bucket.source.id

  rule {
    id     = "expire"
    status = "Enabled"

    filter {}

    expiration {
      days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# ---------------------------------------------------------------- role
resource "aws_iam_role" "build" {
  name        = "${var.name_prefix}-build"
  description = "fukuda-nwc-poc CodeBuild - read the source zip, write logs, push images to ECR"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "build" {
  name = "build"
  role = aws_iam_role.build.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.build.arn}:*"
      },
      {
        Sid      = "Source"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.source.arn}/*"
      },
      {
        Sid      = "EcrToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
        ]
        Resource = "arn:${local.partition}:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}-*"
      },
    ]
  })
}

# ---------------------------------------------------------------- project
# ARM_CONTAINER + aarch64 のイメージなので docker build がそのまま arm64 を作る（buildx も QEMU も要らない）。
# privileged_mode は docker daemon を動かすために要る。
resource "aws_codebuild_project" "build" {
  name           = "${var.name_prefix}-build"
  description    = "Builds the arm64 agent / lab images from the public git repository and pushes them to ECR (fukuda-nwc-poc)"
  service_role   = aws_iam_role.build.arn
  build_timeout  = 30
  queued_timeout = 30

  concurrent_build_limit = 1

  artifacts {
    type = "NO_ARTIFACTS"
  }

  cache {
    type = "NO_CACHE"
  }

  logs_config {
    cloudwatch_logs {
      status     = "ENABLED"
      group_name = aws_cloudwatch_log_group.build.name
    }
  }

  environment {
    type            = "ARM_CONTAINER"
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    privileged_mode = true

    # コンソールの「ビルドの開始」→「環境変数の上書き」で TARGET / IMAGE_TAG を変える
    environment_variable {
      name  = "TARGET" # agent | lab
      value = "agent"
    }
    environment_variable {
      name  = "IMAGE_TAG" # agent と lab-snmpd のタグ（同じタグは push できない。更新のたびに変える）
      value = "v1"
    }
    environment_variable {
      name  = "NAME_PREFIX"
      value = var.name_prefix
    }
    environment_variable {
      name  = "FRR_TAG"
      value = "10.2.1"
    }
    environment_variable {
      name  = "MULTITOOL_TAG"
      value = "v0.10.0"
    }
    environment_variable {
      name  = "DOCKER_BUILDKIT"
      value = "1"
    }
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.source.bucket}/${var.source_object_key}"
    buildspec = file("${path.module}/buildspec.yml")
  }
}
