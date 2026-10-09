# Runbook: Overseas Expenses Telegram Bot

Operational procedures for the deployed bot. How the system is built is in
[ARCHITECTURE.md](ARCHITECTURE.md); why it is built that way is in
[docs/decisions/](docs/decisions/).

Commands assume the `personal` AWS profile and `ap-southeast-1`. Run them from cmd, or
prefix with `MSYS_NO_PATHCONV=1` in Git Bash — MSYS rewrites a leading `/` in arguments
such as SSM parameter names into a Windows path.

---

## Deploying code

```
uv run python scripts/build_lambda.py
uv run python scripts/deploy_lambda.py              # the bot; add "charts" for the chart function
```

For each function the deploy script uploads the zip to the artifacts bucket in 8 MB parts,
points the function at it, waits for the update to finish, checks Lambda's `CodeSha256`
equals the local archive's, and deletes the staged zip — whether or not the update
succeeded; the bucket's one-day expiry catches a process that dies first. See
[0009](docs/decisions/0009-deploy-through-temporary-artifacts-bucket.md) for why it does
not upload directly.

It needs nothing per machine beyond what Terraform already uses: names, bucket, region
and the target account come from `terraform output`, and the profile from `--profile`,
`AWS_PROFILE`, or `aws_profile` in `terraform/local.auto.tfvars`, in that order. It
refuses to run when the credentials belong to another account — the default profile on
the development laptop is a different account.

---

## Rotating the Telegram bot token

Five steps, and the last two are the ones that bite. Skipping step 4 means nothing reaches
the bot at all; skipping step 5 means messages arrive, the Lambda runs, the gateway logs a
cheerful 200, and every reply dies with `telegram.error.InvalidToken: Unauthorized`.

1. **Revoke and reissue in BotFather.** This also **clears the webhook registration** —
   `getWebhookInfo` afterwards reports an empty `url`. The bot ID is unchanged, but the
   delivery target is gone.
2. **Update `.env`** for local polling.
3. **Update the SSM parameter** so production gets the new value:
   ```
   aws ssm put-parameter --name /ExpensesCalculatorAgenticBot/telegram-bot-token --value "<new token>" --type SecureString --overwrite --profile personal --region ap-southeast-1
   ```
   In Git Bash without `MSYS_NO_PATHCONV=1`, this silently creates a parameter under the
   mangled name while the real one keeps its old value.
4. **Re-register the webhook** with the existing secret — it is independent of the bot
   token. The steps in [Rotating the webhook URL](#rotating-the-webhook-url) step 2 do
   this without printing either secret.
5. **Force a Lambda cold start.** `settings = Settings()` runs at module import and SSM is
   read once per cold start, so warm containers keep serving the previous token until they
   recycle. Re-deploying is the cleanest trigger — Terraform has `ignore_changes` on
   `filename`, so it causes no drift:
   ```
   uv run python scripts/deploy_lambda.py
   ```
   Caching secrets at import is deliberate — fetching them per invocation would add SSM
   latency and cost to every message — but it does mean any secret rotation needs a deploy
   to take effect.

Rotating the **webhook secret** is steps 3–5 with the `webhook-secret` parameter, and the
same cold-start requirement applies for the same reason.

---

## Rotating the webhook URL

The URL is `https://<api-id>.execute-api…/webhook`, and the API id is random. It is not a
credential — the secret token is — but a known URL invites traffic that costs a Lambda
invocation per request, even when rejected with 403. Replacing the gateway issues a new
id. Done 9 Oct 2026 after the old id was found in the repository's public history.

1. In `terraform/`: `terraform apply -replace=aws_apigatewayv2_api.webhook`. Expect 5 to
   add and 5 to destroy — the API, integration, route, stage and the Lambda permission
   that names the API. Destroy runs first, so deliveries fail until step 2; Telegram
   holds them and retries, so they arrive late rather than being lost.
2. Point Telegram at the new URL with the existing secret, in PowerShell from
   `terraform/`, reading the secrets into variables so they are never printed:
   ```powershell
   $url    = terraform output -raw webhook_url
   $token  = aws ssm get-parameter --name /ExpensesCalculatorAgenticBot/telegram-bot-token --with-decryption --query Parameter.Value --output text --profile personal --region ap-southeast-1
   $secret = aws ssm get-parameter --name /ExpensesCalculatorAgenticBot/webhook-secret --with-decryption --query Parameter.Value --output text --profile personal --region ap-southeast-1
   Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/setWebhook" -Body @{ url = $url; secret_token = $secret }
   ```
   Omit `drop_pending_updates`, so updates queued during the switch are still delivered.
3. Verify: the old host no longer resolves, a POST to the new URL without the secret
   returns 403, and a real message gets a reply.

Never write the URL into the repository; `terraform output` is its only record.

---

## Diagnosing a silent bot

Work outward from Telegram, since each layer fails differently:

| Check | What it tells you |
|---|---|
| `getWebhookInfo` → empty `url` | Telegram has no delivery target; re-run `setWebhook` |
| `getWebhookInfo` → `last_error_message` | Telegram reached the gateway and got an error back |
| API Gateway access log, no entries | The delivery never arrived — DNS, registration, or Telegram-side |
| Access log `status: 403` | The secret token did not match; the handler rejected it before processing |
| Access log `status: 503`, latency ≈ 30000 | The turn ran past the gateway's 30 s limit; Telegram will redeliver, and the `update_id` claim drops the repeat |
| Access log 200 but no reply in Telegram | The Lambda ran and failed after acknowledging — read its log |
| Lambda log `InvalidToken: Unauthorized` | Warm container holding a stale token; force a cold start (token rotation, step 5) |

Everything the application logs below `ERROR` depends on the explicit `setLevel` in
`configure_logging` (`src/bot/logging_setup.py`): `logging.basicConfig` does nothing once
the Lambda runtime has attached a handler, so without it the root logger sits at
`WARNING` and every `logger.info` — the per-turn record included — is dropped in
production while polling looks fine.

---

## Reading a conversation from the logs

Every turn the bot runs writes one JSON log line with `event = "turn"`: the chat, the
sender, the user's message, each tool call with its arguments and result, the reply,
model calls and tokens, timings, and the outcome (`replied`, `confirmation_asked`,
`end_trip_failed`, `error`). Fields are described in `src/bot/turn_log.py`; the decision
is [0014](docs/decisions/0014-structured-logs-with-a-record-per-turn.md). Lines are kept
for the log group's 60 days.

Run a Logs Insights query from the CLI (`--start-time` and `--end-time` are epoch
seconds; the query runs asynchronously, so fetch its results with the returned id):

```bash
aws logs start-query --profile personal --region ap-southeast-1 \
  --log-group-name /aws/lambda/ExpensesCalculatorAgenticBot \
  --start-time $(date -d '2 days ago' +%s) --end-time $(date +%s) \
  --query-string '<query>'
aws logs get-query-results --profile personal --region ap-southeast-1 --query-id <id>
```

| To see | Query |
|---|---|
| One chat's conversation | `filter event = "turn" and chat = "<chat id>" \| fields @timestamp, user_message, reply \| sort @timestamp asc` |
| Turns that failed | `filter event = "turn" and outcome in ["error", "end_trip_failed"] \| fields @timestamp, chat, user_message, error` |
| A tool's calls, with arguments | `filter event = "turn" and steps.0.tool = "edit_expense" \| fields @timestamp, chat, user_message, steps.0.args.expense_id, steps.0.result` |
| Slow turns | `filter event = "turn" and timings_ms.graph > 20000 \| fields @timestamp, chat, user_message, timings_ms.graph, model_calls` |
| Errors with tracebacks | `filter level = "ERROR" \| fields @timestamp, message, exception` |

In Git Bash on Windows, prefix both commands with `MSYS_NO_PATHCONV=1 PYTHONUTF8=1`.
Without the first, Git Bash rewrites `/aws/lambda/...` into a Windows file path and the
query is refused as unauthorised on that path; without the second, the CLI fails to print
the emoji in replies.

Logs Insights flattens nested JSON with dots and array positions (`steps.0.tool`), and
discovers a bounded number of fields per line. When a field is missing from results,
read the whole line with `fields @message`.

---

## Setting up Terraform on a new machine

State is in S3 ([0004](docs/decisions/0004-terraform-state-in-s3-bootstrap.md)), so a
fresh clone needs only the two gitignored local files, in both `terraform/` and
`terraform/bootstrap/`:

- `local.auto.tfvars` — `aws_account_id` and `aws_profile`; see the note in
  `terraform/terraform.tfvars`
- `backend.local.hcl` — `profile = "<the same profile>"`. Backend blocks cannot read
  variables, so the backend needs the profile given separately

Then, in each directory:

```
terraform init -backend-config=backend.local.hcl
terraform plan
```

`plan` should show no changes. If it proposes creating resources that already exist, it is
not reading the S3 state — check the init output named the `s3` backend.

A machine that cloned before 9 Oct 2026 holds rewritten history: run `git fetch origin`
and `git reset --hard origin/main` once, after confirming it has no unpushed work.

---

## Restoring the expenses table

Point-in-time recovery keeps the table restorable to any second in the last 35 days. A
restore never overwrites the live table — it creates a new one — so the usual repair is
to copy the affected items back, not to switch the bot over: the Lambda's IAM policy is
scoped to the live table's ARN, and Terraform manages that table by name.

1. Pick a time just before the damage, in UTC. The bot's logs and the item's
   `updated_at` help; `describe-continuous-backups` shows the earliest restorable time.
2. Restore into a new table:
   ```
   aws dynamodb restore-table-to-point-in-time --source-table-name ExpensesCalculator --target-table-name ExpensesCalculator-restore-<yyyymmdd> --restore-date-time <2026-10-09T05:00:00Z> --profile personal --region ap-southeast-1
   aws dynamodb wait table-exists --table-name ExpensesCalculator-restore-<yyyymmdd> --profile personal --region ap-southeast-1
   ```
3. Read the items you need from the restored table (`get-item` or `query` on the
   ledger's `PK`) and write them back to `ExpensesCalculator` with `put-item`. Check the
   ledger with "show all" afterwards.
4. Delete the restored table once done — it is billed as a table of its own, and it is
   not managed by Terraform.

A restored table does not carry over every setting of its source; to our understanding
(unverified) TTL, PITR itself and tags must be re-enabled by hand. That does not matter
for a short-lived copy used only to read items back.

An ended trip is easier: its expenses are in the `ARCHIVE#` item for 90 days
([0010](docs/decisions/0010-archive-trip-before-delete.md)), readable with a `query` on the
ledger's `PK` and `begins_with(SK, "ARCHIVE#")`.

---

## Recovering Terraform state

Each config's state is one object in `expenses-bot-tfstate-ojg0cd`: `bot/terraform.tfstate`
and `bootstrap/terraform.tfstate`. If it is deleted or overwritten by a bad apply, restore a
previous version first — versioning keeps superseded versions for 90 days:

```
aws s3api list-object-versions --bucket expenses-bot-tfstate-ojg0cd --prefix bot/terraform.tfstate
aws s3api copy-object --bucket expenses-bot-tfstate-ojg0cd --key bot/terraform.tfstate --copy-source "expenses-bot-tfstate-ojg0cd/bot/terraform.tfstate?versionId=<VERSION_ID>"
```

If no usable version exists — in practice, only if the bucket itself was deleted — the
resources are still running in AWS but Terraform has no record of them, and would plan to
create all of them again. The last resort is to import each one into a fresh state — the
resource blocks in `terraform/` name what to import — and not to apply anything until
`terraform plan` shows no changes.
