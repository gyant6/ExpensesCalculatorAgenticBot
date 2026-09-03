# ── API Gateway (HTTP API) ────────────────────────────────────────────────────
# Telegram POSTs each update here and API Gateway forwards it to the bot.
#
# An HTTP API rather than a REST API: it is cheaper and simpler, and the control that
# actually authenticates a webhook is the secret token below, which proves the request is
# a genuine Telegram delivery for this bot. REST APIs additionally support resource
# policies — the route to an IP allowlist rejecting non-Telegram traffic before Lambda is
# invoked — and neither resource policies nor WAF are available on an HTTP API. Moving to
# one later means replacing this gateway and re-running setWebhook.

resource "aws_apigatewayv2_api" "webhook" {
  name          = "${local.bot_function_name}-webhook"
  protocol_type = "HTTP"
  description   = "Receives Telegram webhook deliveries for the expenses bot."
}

resource "aws_apigatewayv2_integration" "webhook" {
  api_id = aws_apigatewayv2_api.webhook.id
  # AWS_PROXY hands the whole request to the function rather than mapping fields, so the
  # handler sees the raw Telegram JSON in event["body"] and the headers it must validate.
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.bot.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "webhook" {
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /webhook"
  target    = "integrations/${aws_apigatewayv2_integration.webhook.id}"
}

# No custom domain, so the stage is $default and the URL carries no stage segment.
resource "aws_apigatewayv2_stage" "webhook" {
  api_id      = aws_apigatewayv2_api.webhook.id
  name        = "$default"
  auto_deploy = true

  # Telegram retries an unacknowledged delivery, so a failure that is invisible here is a
  # failure that repeats. One log line per request is enough to see status and latency.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.webhook.arn
    format = jsonencode({
      requestId       = "$context.requestId"
      httpMethod      = "$context.httpMethod"
      path            = "$context.path"
      status          = "$context.status"
      integrationErr  = "$context.integrationErrorMessage"
      responseLatency = "$context.responseLatency"
    })
  }
}

resource "aws_cloudwatch_log_group" "webhook" {
  name              = "/aws/apigateway/${local.bot_function_name}-webhook"
  retention_in_days = 30
}

# Scoped to this API's execution ARN, so nothing else can invoke the bot on its behalf.
resource "aws_lambda_permission" "webhook" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.bot.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*/webhook"
}

# ── Webhook secret token ──────────────────────────────────────────────────────
# Telegram echoes this on every delivery as X-Telegram-Bot-Api-Secret-Token, and the
# handler rejects anything that does not carry it. Without it the gateway URL is the only
# thing standing between the internet and a forged update — one claiming to come from the
# admin's Telegram ID would otherwise pass the auth gate and reach the /auth commands.
#
# Same placeholder-and-ignore pattern as the other secrets, so the real value is set once
# by CLI and never enters Terraform state.

resource "aws_ssm_parameter" "webhook_secret" {
  name  = local.ssm_webhook_secret_path
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}
