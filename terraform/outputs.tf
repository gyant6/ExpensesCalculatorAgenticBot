output "lambda_function_arn" {
  description = "ARN of the Lambda function."
  value       = aws_lambda_function.bot.arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function."
  value       = aws_lambda_function.bot.function_name
}

output "chart_lambda_function_arn" {
  description = "ARN of the chart Lambda function."
  value       = aws_lambda_function.charts.arn
}

output "chart_lambda_function_name" {
  description = "Name of the chart Lambda function. Use with aws lambda update-function-code."
  value       = aws_lambda_function.charts.function_name
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB table."
  value       = aws_dynamodb_table.expenses.name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB table."
  value       = aws_dynamodb_table.expenses.arn
}

output "lambda_exec_role_arn" {
  description = "ARN of the Lambda execution IAM role."
  value       = aws_iam_role.lambda_exec.arn
}

output "artifacts_bucket_name" {
  description = "Bucket scripts/deploy_lambda.py stages zips in."
  value       = aws_s3_bucket.artifacts.id
}

output "aws_account_id" {
  description = <<-EOT
    The account this config deploys to. scripts/deploy_lambda.py refuses to run with
    credentials for any other account. Read from the credentials Terraform used, which
    allowed_account_ids has already checked.
  EOT
  value       = data.aws_caller_identity.current.account_id
}

output "aws_region" {
  description = "Region the functions and the artifacts bucket are in."
  value       = var.aws_region
}

output "webhook_url" {
  description = "Register this with Telegram via setWebhook."
  value       = "${aws_apigatewayv2_api.webhook.api_endpoint}/webhook"
}
