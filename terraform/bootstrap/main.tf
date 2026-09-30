# The bucket that holds Terraform state for this project — the main config's and, once
# migrated, this config's own. Kept apart from the main config so that nothing there, not
# even `terraform destroy`, can delete the bucket its own state lives in.

resource "aws_s3_bucket" "tfstate" {
  bucket = var.state_bucket_name

  # Deleting this bucket deletes every copy of the state for the whole project.
  lifecycle {
    prevent_destroy = true
  }
}

# The S3 backend documentation recommends versioning so a state overwritten by a bad
# apply, or deleted by mistake, can be restored from a previous version.
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

# The state holds resource IDs and ARNs for the whole deployment, so it is encrypted at
# rest. SSE-S3 rather than KMS: KMS would add a per-key monthly charge and a key policy to
# manage, for no access-control benefit here since only this account reads the bucket.
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Disables ACLs entirely, so access is governed by IAM and the bucket policy alone.
resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  # Every apply writes a new version, so without expiry the old ones accumulate forever.
  rule {
    id     = "expire-superseded-state"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Versioning must be on before a noncurrent-version rule means anything.
  depends_on = [aws_s3_bucket_versioning.tfstate]
}

data "aws_iam_policy_document" "tfstate" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.tfstate.arn,
      "${aws_s3_bucket.tfstate.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  policy = data.aws_iam_policy_document.tfstate.json

  # A bucket policy is rejected while Block Public Access is still being applied.
  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}
