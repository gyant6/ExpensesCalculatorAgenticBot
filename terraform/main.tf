terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project = "ExpensesCalculatorAgenticBot"
    }
  }
}

# ── DynamoDB ──────────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "expenses" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# ── SSM Parameter Store (secrets) ─────────────────────────────────────────────
# Terraform creates the parameters with a placeholder value and then ignores
# the value field, so the real secret you set via the AWS console or CLI is
# never overwritten by a subsequent terraform apply and never stored in state.
#
# After terraform apply, set the real values once:
#   aws ssm put-parameter --name /ExpensesCalculatorAgenticBot/telegram-bot-token \
#     --value "<token>" --type SecureString --overwrite --region ap-southeast-1
#   aws ssm put-parameter --name /ExpensesCalculatorAgenticBot/admin-telegram-id \
#     --value "<id>" --type SecureString --overwrite --region ap-southeast-1

locals {
  ssm_telegram_bot_token_path = "/ExpensesCalculatorAgenticBot/telegram-bot-token"
  ssm_admin_telegram_id_path  = "/ExpensesCalculatorAgenticBot/admin-telegram-id"
}

resource "aws_ssm_parameter" "telegram_bot_token" {
  name  = local.ssm_telegram_bot_token_path
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "admin_telegram_id" {
  name  = local.ssm_admin_telegram_id_path
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

# ── IAM — Lambda execution role ───────────────────────────────────────────────

resource "aws_iam_role" "lambda_exec" {
  name = "ExpensesCalculatorAgenticBot-lambda-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_exec_policy" {
  name = "ExpensesCalculatorAgenticBot-lambda-exec-policy"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/lambda/ExpensesCalculatorAgenticBot:*"
      },
      {
        Sid    = "DynamoDB"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:TransactWriteItems"
        ]
        Resource = aws_dynamodb_table.expenses.arn
      },
      {
        Sid      = "Bedrock"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "arn:aws:bedrock:*::foundation-model/${var.bedrock_model_id}"
      },
      {
        Sid    = "SSMSecrets"
        Effect = "Allow"
        Action = ["ssm:GetParameters"]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter${local.ssm_telegram_bot_token_path}",
          "arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter${local.ssm_admin_telegram_id_path}"
        ]
      },
      {
        Sid      = "SSMDecrypt"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${var.aws_region}:${var.aws_account_id}:alias/aws/ssm"
      }
    ]
  })
}

# ── Lambda function ───────────────────────────────────────────────────────────

resource "aws_lambda_function" "bot" {
  function_name    = "ExpensesCalculatorAgenticBot"
  role             = aws_iam_role.lambda_exec.arn
  # function.zip must exist before the first terraform apply.
  # Build it with: bash scripts/build_lambda.sh
  # Subsequent code deploys use: aws lambda update-function-code --zip-file fileb://function.zip
  filename = "${path.module}/../function.zip"
  handler  = "src.bot.main.lambda_handler"
  runtime          = "python3.13"
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_mb

  environment {
    variables = {
      ENVIRONMENT                 = "production"
      AWS_REGION                  = var.aws_region
      AWS_BEDROCK_MODEL_ID        = var.bedrock_model_id
      DYNAMODB_TABLE_NAME         = var.dynamodb_table_name
      LOG_LEVEL                   = "INFO"
      CHECKPOINT_TTL_SECONDS      = "7776000"
      TELEGRAM_BOT_TOKEN_SSM_PATH = local.ssm_telegram_bot_token_path
      ADMIN_TELEGRAM_ID_SSM_PATH  = local.ssm_admin_telegram_id_path
    }
  }


}

resource "aws_cloudwatch_log_group" "bot" {
  name              = "/aws/lambda/ExpensesCalculatorAgenticBot"
  retention_in_days = 30
}
