# Decision records

Decisions that still constrain how this bot is built, one per file, each with the
context that forced it, what was decided, what it costs, and what was rejected. Read the
relevant record before changing something it covers — most exist because the obvious
alternative was tried, measured, or broke in production.

Finished work that constrains nothing is not recorded here; git history has it.

| # | Decision |
|---|---|
| [0001](0001-chart-rendering-in-a-separate-lambda.md) | Render charts in a separate Lambda function |
| [0002](0002-http-api-authenticated-by-secret-token.md) | HTTP API Gateway, authenticated by Telegram's secret token |
| [0003](0003-secrets-and-account-id-out-of-the-repository.md) | Secrets in SSM, account ID and profile out of the repository |
| [0004](0004-terraform-state-in-s3-bootstrap.md) | Terraform state in S3, in a bucket owned by a separate bootstrap config |
| [0005](0005-ledger-scoped-to-the-chat.md) | Scope the expense ledger to the chat, not the sender |
| [0006](0006-expenses-addressed-by-id-with-amount-check.md) | Address expenses by an immutable id, checked against the amount |
| [0007](0007-trip-time-zone-and-local-dates.md) | A time zone per trip; expense dates are local calendar days |
| [0008](0008-redelivered-updates-dropped-by-update-id.md) | Drop redelivered Telegram updates by `update_id` |
| [0009](0009-deploy-through-temporary-artifacts-bucket.md) | Deploy through a temporary, unversioned artifacts bucket |
| [0010](0010-archive-trip-before-delete.md) | Archive a trip's expenses before deleting them |
| [0011](0011-plain-text-replies.md) | Send replies as plain text |
| [0012](0012-versioned-conversation-threads.md) | Versioned conversation threads; check a trip really ended |
