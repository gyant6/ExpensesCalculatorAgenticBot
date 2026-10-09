# 0002. HTTP API Gateway, authenticated by Telegram's secret token

- **Status:** Accepted
- **Date:** 2026-09-03

## Context

Telegram delivers updates by POSTing to a public webhook URL. The URL is not a
credential: anyone who learns it can POST a forged update, including one naming the
admin's Telegram ID and carrying `/auth` commands. An IP allowlist of Telegram's
published ranges was the original plan.

## Decision

- An API Gateway **HTTP API** (`POST /webhook` → Lambda), not a REST API.
- Every delivery must carry `X-Telegram-Bot-Api-Secret-Token` matching the secret
  registered with `setWebhook`, held in SSM. The handler compares it with
  `hmac.compare_digest` before parsing anything, and **fails closed**: with no secret
  configured, every delivery is rejected.
- The Lambda permission is scoped to this API's execution ARN.

## Consequences

- A forged delivery gets a 403 before reaching the graph — verified in production with a
  forged `/auth list` naming the admin.
- No IP allowlist is possible: resource policies exist only on REST APIs, and WAF does
  not support HTTP APIs (checked against the provider schema). The token is the stronger
  control anyway — it proves a request is Telegram's *and* for this bot.
- A rejected request still costs one Lambda invocation. The URL was rotated on
  2026-10-09 after it appeared in the public history (see the runbook); stage throttling
  remains available as a cap.
- The gateway waits at most 30 s for the Lambda; see [0008](0008-redelivered-updates-dropped-by-update-id.md).

## Alternatives considered

- **REST API with an IP allowlist and a weekly CIDR updater.** More moving parts, and an
  IP check cannot tell this bot's deliveries from any other bot's.
- **AWS WAF.** Not available for HTTP APIs, and a recurring cost for what the token
  already does.
