# 0007. A time zone per trip; expense dates are local calendar days

- **Status:** Accepted
- **Date:** 2026-10-09

## Context

The default expense date was the Telegram message's UTC day, while `start_trip` used
Singapore time. The first expense of a September 2026 trip, logged at 06:57 SGT on 12 Sep,
was recorded as 11 Sep — before the trip began. Anywhere behind UTC, an evening expense
landed on the next day. The model was never told the date, so it answered "today" with "I
don't have today's date" and asked for dates it could have defaulted.

## Decision

- **Each trip has an IANA time zone**, stored on `TRIP#ACTIVE`. `start_trip` requires it
  (the bot asks where the user is going if they have not said); `set_trip_timezone`
  changes it mid-trip.
- **Two kinds of time are stored differently.** Moments (`created_at`, `updated_at`) are
  UTC timestamps. The expense `date` is a plain `YYYY-MM-DD` calendar day in the trip's
  zone — converting a calendar day to UTC has no meaning — with the zone stored beside it.
- **One conversion, in code.** `handle_message` passes the message's own UTC time
  (`message_time`). `check_trip_status` converts it once into `local_date`, which both the
  prompt ("Today is Monday, 14 September 2026 (America/Los_Angeles).") and `add_expense`'s
  default use. The model never converts zones; it only does date arithmetic.
- **Tools return through `check_trip_status`**, so a trip started or moved earlier in the
  turn is re-read before the next expense is dated.
- The date line is the prompt's last line, keeping the rest cacheable.
- Prompt rules: never ask for the date; infer the category; default payment to Card.

## Consequences

- With no active trip, the zone is Singapore.
- Expenses already recorded keep their dates when the zone changes.
- Old checkpoints carrying the retired `message_date` key load for an ordinary turn, but
  **not** for resuming an interrupted end-trip: on the first production trip end after
  this change, the resume ran nothing. See
  [0012](0012-versioned-conversation-threads.md).

## Alternatives considered

- **A fixed home zone.** Wrong precisely when the bot is used — abroad.
- **A tool for the model to fetch the date.** An extra round trip per turn for a value
  the prompt can simply state.
