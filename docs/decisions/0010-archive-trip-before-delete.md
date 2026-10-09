# 0010. Archive a trip's expenses before deleting them

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

Ending a trip deleted every expense permanently; the CSV sent to the chat was the only
copy. A trip ended by mistake, or a lost CSV, was unrecoverable.

## Decision

Before deleting anything, `end_trip` writes **one** archive item —
`PK=USER#<ledger>`, `SK=ARCHIVE#<ended_at>` — holding every expense in full plus
`start_date`, `ended_at` and a `ttl` 90 days out. Only after that write succeeds are the
expenses and `TRIP#ACTIVE` deleted. An empty trip writes no archive.

## Consequences

- A single put lands whole or not at all; if it fails, nothing is deleted.
- DynamoDB's 400 KB item limit caps a trip at roughly a thousand expenses; beyond that
  the put fails and the trip cannot end until the design changes.
- The `ARCHIVE#` prefix is outside every `EXPENSE#` query, so the next trip never sees it.
- TTL deletion is lazy — typically within days of expiry.
- Nothing in the bot reads archives yet; recovery is by hand (see the runbook).
- Point-in-time recovery covers what this does not: bad edits mid-trip.

## Alternatives considered

- **Move each expense to an archive key.** As many writes as expenses, and doing it
  atomically means multi-item transactions with their own size caps.
