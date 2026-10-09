# 0008. Drop redelivered Telegram updates by `update_id`

- **Status:** Accepted
- **Date:** 2026-10-09

## Context

The HTTP API waits at most 30 s for the Lambda, then returns 503; Telegram treats that as
undelivered and sends the update again, to a fresh invocation. Observed twice: a 29 Sep
"show all" ran twice (the user saw an error and then the list), and a 7 Oct trip end ran
34.5 s and was retried — caught only by the confirmation claim. A retried "add expense"
would record it twice.

## Decision

- Before processing, `lambda_handler` claims the update with a conditional put of
  `PK=UPDATE#<update_id>`, `SK=MARKER` and a one-day `ttl`. If the marker exists, the
  delivery is acknowledged with 200 and dropped before any model call. Authentication
  runs first, so a forger cannot burn a genuine id.
- `update_id` is unique per update — messages and button taps alike — and repeated
  unchanged on redelivery. It is a separate item from any expense: one update can create
  several expenses, and most create none.
- **At most once, deliberately.** If the first run fails midway, its redelivery is
  dropped too: a lost message is visible (no reply), a duplicate expense is silent. A
  failed claim (DynamoDB unavailable) fails the invocation, so Telegram retries.

## Consequences

- Duplicates are prevented; slow turns still exceed 30 s and can show the generic error
  before the real reply. Shortening turns (bounding the context) is the first remedy.
- Local polling needs no claim — Telegram does not redeliver to polling.

## Alternatives considered

- **The message timestamp as the key.** Telegram sends it to the second, so two messages
  in one second would collide; button taps carry the time of the message they belong to.
- **Acknowledge at once and process through an SQS FIFO queue** (message group = chat).
  Removes the 30 s limit and serialises each chat's turns. Deferred, not rejected: it is
  the next step if slow turns or concurrent-turn clashes persist.
