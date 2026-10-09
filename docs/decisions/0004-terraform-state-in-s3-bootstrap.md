# 0004. Terraform state in S3, in a bucket owned by a separate bootstrap config

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

Terraform state was a gitignored local file, so it existed only on the machine that last
ran `apply`. Another machine saw no state and would have planned to create every resource
again — which happened in practice.

## Decision

- State lives in `expenses-bot-tfstate-ojg0cd`: `bot/terraform.tfstate` for `terraform/`,
  `bootstrap/terraform.tfstate` for `terraform/bootstrap/`.
- **The bucket belongs to its own config**, `terraform/bootstrap/`, with
  `prevent_destroy`. Nothing in the main config — `terraform destroy` included — can
  delete the bucket holding its own state. The bootstrap's first apply necessarily ran
  on local state, then migrated into the bucket it created.
- Versioning on, superseded versions kept 90 days; SSE-S3; all Block Public Access flags;
  `BucketOwnerEnforced`; a policy denying non-TLS requests.
- Locking with `use_lockfile`, not a DynamoDB table — the S3 backend docs mark
  `dynamodb_table` deprecated. `required_version >= 1.10`.
- The name carries a random suffix rather than the account ID, which stays out of the
  repository ([0003](0003-secrets-and-account-id-out-of-the-repository.md)).

## Consequences

- Any machine with the two gitignored local files runs `terraform init` and sees the same
  state (see the runbook).
- A bad apply or a deleted state object is recoverable from a previous version.
- **This bucket is for state only.** Deploy artefacts go elsewhere
  ([0009](0009-deploy-through-temporary-artifacts-bucket.md)): deploys need write and delete
  permissions that must never apply to state.

## Alternatives considered

- **Create the bucket by CLI.** Its settings would live in no code.
- **Manage the bucket in the main config.** That config would hold the bucket its own
  state lives in, and a `destroy` would delete it.
