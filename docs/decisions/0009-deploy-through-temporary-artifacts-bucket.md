# 0009. Deploy through a temporary, unversioned artifacts bucket

- **Status:** Accepted
- **Date:** 2026-10-09

## Context

`aws lambda update-function-code --zip-file` sends the 47 MB archive as one request. On
2026-10-09 it repeatedly stalled mid-upload and failed whole, on two networks, while
45 MB test uploads sometimes succeeded — an intermittent link, not a size limit.

## Decision

- `scripts/deploy_lambda.py` uploads each archive to `expenses-bot-artifacts-ojg0cd` in
  8 MB parts that retry individually, calls `update-function-code` with the S3 key, waits
  for the update, checks Lambda's `CodeSha256` equals the local archive's, and **deletes
  the staged zip** whether or not the update succeeded.
- **The bucket is unversioned**, so a delete really removes the zip, and a one-day
  expiry rule catches a deploy that dies between upload and delete. Encrypted, public
  access blocked, TLS-only, `force_destroy` on.
- **Separate from the state bucket** ([0004](0004-terraform-state-in-s3-bootstrap.md)).
- The script hardcodes nothing per machine: names, bucket, region and account come from
  `terraform output`; the profile from `--profile`, `AWS_PROFILE`, or `aws_profile` in
  `terraform/local.auto.tfvars`. It refuses credentials for any account other than the
  one Terraform manages.
- Terraform owns the infrastructure and the script owns the code:
  `ignore_changes = [filename, source_code_hash]` on both functions.

## Consequences

- The bucket is empty between deploys and costs nothing measurable.
- There is no stored artefact to roll back to; a rollback is a rebuild from the wanted
  commit.
- CI (Phase 3) deploys with the same script.

## Alternatives considered

- **Reuse the state bucket under a prefix.** Its versioning keeps "deleted" zips for 90
  days, and truly removing them needs `s3:DeleteObjectVersion` — the permission that can
  destroy old copies of the state. Deploys, and later CI, must never hold it there.
- **Keep retrying the direct upload.** Worked intermittently; not a procedure.
