resource "aws_iam_role" "lab" {
  name        = "${var.name_prefix}-lab"
  description = "fukuda-nwc-poc lab EC2 - SSM managed node, pull lab images from ECR, read lab/ from the asset bucket"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${var.name_prefix}-lab" }
}

resource "aws_iam_role_policy_attachment" "lab_ssm" {
  role       = aws_iam_role.lab.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "lab_assets" {
  name = "lab-assets"
  role = aws_iam_role.lab.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrPull"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = "arn:${local.partition}:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}-lab-*"
      },
      {
        Sid      = "EcrToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "S3Read"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:${local.partition}:s3:::${local.bucket}/lab/*"
      },
      {
        Sid      = "S3List"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:${local.partition}:s3:::${local.bucket}"
        Condition = {
          StringLike = { "s3:prefix" = "lab/*" }
        }
      },
    ]
  })
}

resource "aws_iam_instance_profile" "lab" {
  name = "${var.name_prefix}-lab"
  role = aws_iam_role.lab.name
}
