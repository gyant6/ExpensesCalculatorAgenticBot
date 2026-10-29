# 0015. Trip insights from a separate model, with figures computed in code

- **Status:** Accepted
- **Date:** 2026-10-29

## Context

The trip-end message was five plain lines: a total and one line per category. The owner
wanted real analysis in it, in Zuzu's voice. The totals had earlier been moved out of the
model and into code because the model's own sums were wrong
([ARCHITECTURE.md](../../ARCHITECTURE.md), "Calculate end-of-trip totals in code"), so
simply asking the chat model to write the summary again was ruled out.

Two further constraints. Ending a trip deletes the expenses before the summary is built, so
nothing that follows may be able to undo or block the ending. And the HTTP API waits at
most 30 seconds for a response ([0008](0008-redelivered-updates-dropped-by-update-id.md)),
so a slow extra model call must not be able to damage what the user already received.

## Decision

- **The summary, charts and CSV are unchanged and sent first.** Their totals stay computed
  in code from the delivered CSV.
- **A separate model writes one short follow-up message**, after the files are delivered
  and the thread is cleared. It is Claude Sonnet 4.6 through the `global.` inference
  profile, configured by `AWS_BEDROCK_ANALYSIS_MODEL_ID`; unset turns the feature off.
  It is one prompt and one call with no tools bound, not an agent: nothing it is told can
  make it act on anything.
- **Code computes every figure** (`trip_facts.py`) from the delivered CSV: totals,
  category shares, per-day spending, the largest expenses, currencies, payment methods.
  The prompt (`trip_analysis_prompt.py`) leaves the choice of observations to the model
  and forbids arithmetic.
- **The reply is checked before it is sent** (`trip_analysis.find_problem`): every number
  must appear in the fact sheet, compared by value; no multiplier comparisons; no
  markdown; at most 1,200 characters. A reply that fails is dropped and logged in full.
- **Best effort.** Any provider failure, timeout (12 s, no retries), failed check or
  Telegram error leaves the user with what they already have. The call's tokens and
  timing, and the message itself, go in the turn record
  ([0014](0014-structured-logs-with-a-record-per-turn.md)).
- **Free text is cleaned before it reaches the prompt:** whitespace collapsed, angle
  brackets stripped, length cut, and the facts fenced in `<trip_facts>` tags the prompt
  declares to be data.

## Consequences

- A new paid call at every trip end, and a few seconds more in the callback. A trial call
  took about 4 s. The message follows everything else, so the user waits for nothing.
- Some replies will be dropped. In a trial of 13 calls with the final prompt, 3 were, all
  on the largest sample trip, mostly from the model adding percentages. The log shows each
  one, and `scripts/preview_trip_analysis.py` re-runs the sample trips to tune the prompt.
- The checks have a known gap. They verify numbers, not claims: a false statement with no
  number passes, and so does a number that happens to occur elsewhere in the fact sheet.
  Earlier prompts produced "second-busiest day" (it was third) and exchange rates
  "moving"; the prompt now forbids both, and only reading real messages shows whether that
  holds.
- Bedrock permission is granted by model ARN, and the template recognises only the
  `global`, `us`, `eu` and `apac` profile prefixes. An `au.` profile would be treated as a
  foundation model and denied at runtime, so the `global.` profile is used.
- Sonnet 4.5 was the first suggestion but is `LEGACY` in Bedrock. Sonnet 5.5 is listed but
  not usable on this account.

## Alternatives considered

- **Code-built colourful layout** with emoji bars and fixed commentary. Safe and free, but
  the commentary can only say what a template allows. Kept as the fallback shape: the
  fact sheet already holds what it would need.
- **The chat model writes the whole summary.** Rejected: that is what produced wrong totals.
- **A tool-using agent** that queries the archive itself. More flexible, but it brings back
  the risk of quoting figures it fetched wrongly, and a one-off summary does not need it.
- **A stricter check that rejects any figure the model did not copy verbatim from a
  sentence.** Would catch the coincidental-number gap, but drops far more replies; revisit
  if real trips show false claims getting through.
