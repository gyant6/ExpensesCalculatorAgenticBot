data "aws_caller_identity" "current" {}

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

  # Named once here because each appears in the function, its log group, and the IAM
  # policy that scopes writes to that log group.
  bot_function_name   = "ExpensesCalculatorAgenticBot"
  chart_function_name = "ExpensesCalculatorAgenticBot-charts"

  # Strips the cross-region routing prefix, so global.anthropic.claude-haiku-4-5... names
  # the foundation model anthropic.claude-haiku-4-5... that the profile routes to.
  bedrock_foundation_model_id = replace(var.bedrock_model_id, "/^(global|us|eu|apac)\\./", "")
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
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.bot_function_name}:*"
      },
      {
        # Scoped to the chart function alone. This is the only cross-function permission
        # the bot holds, and charts are the only thing it may ask that function to do.
        Sid      = "InvokeChartFunction"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.charts.arn
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
        # Invoking through an inference profile is authorised against both the profile
        # and every foundation model it can route to, so granting only one of the two
        # fails at runtime. The region wildcard covers a global profile's routing.
        Sid    = "Bedrock"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
          "arn:aws:bedrock:*::foundation-model/${local.bedrock_foundation_model_id}"
        ]
      },
      {
        Sid    = "SSMSecrets"
        Effect = "Allow"
        Action = ["ssm:GetParameters"]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_telegram_bot_token_path}",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_admin_telegram_id_path}"
        ]
      },
      {
        Sid      = "SSMDecrypt"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      }
    ]
  })
}

# ── IAM — chart Lambda execution role ─────────────────────────────────────────
# A separate role because the chart function is a pure function of its input: expenses
# and rates in, PNG bytes out. It reads no database, calls no model and holds no secrets,
# so writing its own logs is the only permission it needs. Reusing the bot's role would
# hand a renderer full read/write access to every expense.

resource "aws_iam_role" "chart_lambda_exec" {
  name = "ExpensesCalculatorAgenticBot-chart-lambda-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "chart_lambda_exec_policy" {
  name = "ExpensesCalculatorAgenticBot-chart-lambda-exec-policy"
  role = aws_iam_role.chart_lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "CloudWatchLogs"
      Effect = "Allow"
      Action = [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ]
      Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.chart_function_name}:*"
    }]
  })
}

# ── Lambda functions ──────────────────────────────────────────────────────────
# Terraform creates these functions; the AWS CLI deploys code into them. The lifecycle
# blocks below are what keep those two responsibilities from fighting: without them, a
# terraform apply after a CLI deploy would see prod differing from the zip on disk and
# roll production back to whatever was last built locally.
#
# Both archives must exist before the first apply. Build them with:
#   uv run python scripts/build_lambda.py
# Subsequent code deploys:
#   aws lambda update-function-code --function-name <name> --zip-file fileb://<archive>

resource "aws_lambda_function" "bot" {
  function_name = local.bot_function_name
  role          = aws_iam_role.lambda_exec.arn
  filename      = "${path.module}/../function.zip"
  handler       = "src.bot.main.lambda_handler"
  runtime       = "python3.13"
  architectures = ["x86_64"]
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory_mb

  environment {
    variables = {
      # AWS_REGION is deliberately absent: it is a reserved Lambda environment variable,
      # set by the runtime, and supplying it here is rejected at deploy time.
      ENVIRONMENT                  = "production"
      AWS_BEDROCK_MODEL_ID         = var.bedrock_model_id
      DYNAMODB_TABLE_NAME          = var.dynamodb_table_name
      LOG_LEVEL                    = "INFO"
      CHECKPOINT_TTL_SECONDS       = "7776000"
      TELEGRAM_BOT_TOKEN_SSM_PATH  = local.ssm_telegram_bot_token_path
      ADMIN_TELEGRAM_ID_SSM_PATH   = local.ssm_admin_telegram_id_path
      CHART_LAMBDA_FUNCTION_NAME   = local.chart_function_name
      CHART_LAMBDA_TIMEOUT_SECONDS = tostring(var.chart_client_timeout)
    }
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_lambda_function" "charts" {
  function_name = local.chart_function_name
  role          = aws_iam_role.chart_lambda_exec.arn
  filename      = "${path.module}/../chart_function.zip"
  handler       = "src.bot.chart_handler.lambda_handler"
  runtime       = "python3.13"
  architectures = ["x86_64"]
  timeout       = var.chart_lambda_timeout
  memory_size   = var.chart_lambda_memory_mb

  environment {
    variables = {
      # Everything this function needs. It never loads config.py, which would demand the
      # bot token and admin ID it has no business holding.
      LOG_LEVEL = "INFO"
    }
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }
}

resource "aws_cloudwatch_log_group" "bot" {
  name              = "/aws/lambda/${local.bot_function_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "charts" {
  name              = "/aws/lambda/${local.chart_function_name}"
  retention_in_days = 30
}
