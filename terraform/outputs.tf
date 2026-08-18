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
