# 0005. Scope the expense ledger to the chat, not the sender

- **Status:** Accepted
- **Date:** 2026-09-03

## Context

Expenses were keyed by the sender's Telegram user ID. In a group, each member therefore
had a private ledger inside a shared conversation: two people logging the same trip saw
different lists and different totals, which defeats the point of a group.

## Decision

The ledger is the chat: `ledger_id = str(chat.id)`, so `PK = USER#<chat id>`. In a private
chat that equals the user's ID, so existing private ledgers were unaffected. Access control
stays per entity (`AUTH#<user or group id>`). The conversation thread is per chat too.

## Consequences

- A group shares one trip, one list and one summary.
- Shared state brings shared-state problems, recorded on the roadmap: two members'
  concurrent turns write the same checkpoint thread, expenses record no author, and the
  `USER#` prefix now holds group IDs (renaming it is a data migration, deferred until the
  table is next empty).
- Positional edit targets became unsafe across members — resolved by
  [0006](0006-expenses-addressed-by-id-with-amount-check.md).

## Alternatives considered

- **Keep per-sender ledgers and merge at trip end.** Members would still see different
  lists all trip, and edits by one could not touch another's expenses.
