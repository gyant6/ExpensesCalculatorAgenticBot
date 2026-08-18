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

# The three chart timeouts must stay ordered:
#   chart_lambda_timeout < chart_client_timeout < lambda_timeout
# so a slow render is abandoned by the chart function first, then by the bot's client,
# leaving the bot alive to deliver the trip summary without charts. Inverting the order
# means a slow render kills the invocation that was carrying the user's summary.

variable "chart_lambda_timeout" {
  description = "Chart Lambda timeout in seconds. Must be below chart_client_timeout."
  type        = number
}

variable "chart_lambda_memory_mb" {
  description = <<-EOT
    Chart Lambda memory in MB. Lambda scales CPU with memory, so under-provisioning
    shows up as slow matplotlib renders rather than as errors.
  EOT
  type        = number
}

variable "chart_client_timeout" {
  description = <<-EOT
    Read timeout in seconds the bot applies when invoking the chart Lambda, passed
    through as CHART_LAMBDA_TIMEOUT_SECONDS. Must sit between chart_lambda_timeout and
    lambda_timeout.
  EOT
  type        = number
}
