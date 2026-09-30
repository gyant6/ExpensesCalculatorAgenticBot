variable "aws_region" {
  description = "AWS region for the state bucket. Must match the region in both backend blocks."
  type        = string
}

variable "aws_account_id" {
  description = <<-EOT
    The account the bucket belongs in. Compared against the credentials in use, never
    interpolated. Set in the gitignored local.auto.tfvars, as for the main config.
  EOT
  type        = string
}

variable "aws_profile" {
  description = "Named AWS profile to authenticate with. Null uses the standard credential chain."
  type        = string
  default     = null
}

variable "state_bucket_name" {
  description = <<-EOT
    Name of the bucket holding Terraform state for this project. S3 names are global, so
    it carries a random suffix rather than the account ID. Must match the bucket named in
    both backend blocks, which cannot read variables.
  EOT
  type        = string
}

variable "noncurrent_version_retention_days" {
  description = <<-EOT
    Days a superseded state version is kept before S3 deletes it. These old versions are
    the recovery path for a corrupted or mistaken apply.
  EOT
  type        = number
}
