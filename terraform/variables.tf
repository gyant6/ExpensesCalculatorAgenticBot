variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
}

variable "aws_account_id" {
  description = "AWS account ID, used to scope IAM policies."
  type        = string
}

variable "dynamodb_table_name" {
  description = "DynamoDB table name. Must match DYNAMODB_TABLE_NAME in Lambda env."
  type        = string
}

variable "bedrock_model_id" {
  description = "Bedrock model ID. Must match AWS_BEDROCK_MODEL_ID in Lambda env."
  type        = string
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds. Must cover worst-case LLM + DynamoDB latency."
  type        = number
}

variable "lambda_memory_mb" {
  description = "Lambda memory in MB."
  type        = number
}
