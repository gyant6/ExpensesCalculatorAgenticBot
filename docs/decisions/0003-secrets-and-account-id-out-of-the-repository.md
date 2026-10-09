# 0003. Secrets in SSM; account ID and profile out of the repository

- **Status:** Accepted
- **Date:** 2026-09-03

## Context

The repository is public. The bot needs a Telegram token, the admin's Telegram ID and a
webhook secret at runtime, and Terraform needs to know which AWS account it may touch —
on a laptop whose default profile is a different, work account.

## Decision

- **Secrets live in SSM Parameter Store** as SecureStrings. Terraform creates each with a
  `REPLACE_ME` placeholder and `ignore_changes = [value]`, so the real value — set by
  CLI — is never overwritten and never enters Terraform state. Lambda environment
  variables hold only the parameter paths; `config.py` fetches the values at cold start
  when `ENVIRONMENT=production`.
- **The AWS account ID and profile are gitignored**, in `terraform/local.auto.tfvars` and
  `backend.local.hcl`. The provider sets `allowed_account_ids`, so Terraform refuses to
  plan with credentials for any other account. ARNs are built from
  `data.aws_caller_identity`, never from the variable.
- **`terraform.tfvars` and `.terraform.lock.hcl` are committed** — nothing in them is
  sensitive, and without them the repository cannot reproduce its infrastructure.
- **Real identities stay out of tests and docs**: fictional IDs and names only. History
  was rewritten on 2026-10-09 to remove real ones.

## Consequences

- Secrets are cached per cold start, so rotating one needs a deploy (see the runbook).
- A fresh clone fails asking for the account ID rather than planning against whatever
  credentials happen to be default.
- `scripts/deploy_lambda.py` applies the same account check before deploying.

## Alternatives considered

- **Lambda environment variables for secrets.** Visible to anyone who can read the
  function configuration, and stored in Terraform state.
- **Fetching secrets per invocation.** Adds SSM latency and cost to every message for
  rotations that happen rarely.
