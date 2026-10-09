# 0011. Send replies as plain text

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

Replies were sent with `parse_mode="HTML"` whenever they contained `<`. That dated from a
prompt that once asked for a `<pre>` table — removed in the same change that added the
check, and replaced with "Always reply in plain text". From then on, any `<` was literal
text ("expenses < 10 SGD"), which Telegram rejected as an unsupported tag, losing the
reply.

## Decision

Every reply goes out with no parse mode. Long replies are split at newlines into chunks
within Telegram's 4096-character limit; a callback's first chunk edits the confirmation
message and the rest are sent as replies.

## Consequences

- A literal `<` can no longer break a reply; tests assert no reply carries a parse mode.
- No formatting is available. If it is ever wanted, the prompt must ask for HTML
  explicitly and the sender must escape or fall back — not infer HTML from content.

## Alternatives considered

- **Keep HTML and resend as plain text when Telegram rejects it.** Deployed briefly; it
  caught the symptom of a mode nothing used.
