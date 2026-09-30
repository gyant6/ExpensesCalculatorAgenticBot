output "state_bucket_name" {
  description = "Bucket holding Terraform state. Must match the bucket in both backend blocks."
  value       = aws_s3_bucket.tfstate.id
}
