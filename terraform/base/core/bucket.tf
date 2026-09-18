# ---------------------------------------------------------------- shared S3 bucket
# web/（画面のコードと wheel）、docs/（KB の取り込み元。terraform/agent の create_knowledge_base = true のとき）、lab/ stream/ analytics/ を置く。
# 名前は kb のまま（ナレッジベースを作らなくても使う）。
# force_destroy = true なので、docs/ web/ lab/ stream/ が残っていても terraform destroy で消える
resource "aws_s3_bucket" "kb" {
  bucket        = "${local.name_prefix}-kb-${local.account_id}"
  force_destroy = true

  tags = { Name = "${local.name_prefix}-kb" }
}

resource "aws_s3_bucket_public_access_block" "kb" {
  bucket                  = aws_s3_bucket.kb.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "kb" {
  bucket = aws_s3_bucket.kb.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "kb" {
  bucket = aws_s3_bucket.kb.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_policy" "kb" {
  bucket = aws_s3_bucket.kb.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.kb.arn, "${aws_s3_bucket.kb.arn}/*"]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.kb]
}
