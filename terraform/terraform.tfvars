# Deployment configuration, committed on purpose: this is the description of what is
# actually deployed, and keeping it on one machine would leave the repo unable to
# reproduce its own infrastructure.
#
# Nothing secret belongs here. TELEGRAM_BOT_TOKEN and ADMIN_TELEGRAM_ID live in SSM
# Parameter Store, set by CLI after the first apply, and never enter Terraform state.
# The account ID is not here either — it is read from the credentials in use via
# data.aws_caller_identity.

aws_region = "ap-southeast-1"

dynamodb_table_name = "ExpensesCalculator"
bedrock_model_id    = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

# The bot's own ceiling. Must cover the worst case of a trip end: a Bedrock call, the FX
# fetch, the chart invoke, then a second Bedrock call to write the summary.
lambda_timeout   = 120
lambda_memory_mb = 1024

# Chart rendering. Keep the ordering chart_lambda_timeout < chart_client_timeout <
# lambda_timeout, so a slow render is abandoned by the chart function first and by the
# bot's client second, leaving the bot alive to send the summary without charts.
chart_lambda_timeout   = 25
chart_client_timeout   = 30
chart_lambda_memory_mb = 1024
