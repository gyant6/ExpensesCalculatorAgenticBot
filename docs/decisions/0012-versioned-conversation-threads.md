# 0012. Versioned conversation threads, and checking a trip really ended

- **Status:** Accepted
- **Date:** 2026-10-09

## Context

The first trip end after [0007](0007-trip-time-zone-and-local-dates.md) shipped ran on a
thread whose earlier checkpoint had been written by the previous code, carrying the
retired `message_date` key. The turn reached the end-trip confirmation normally, but the
resume — `graph.invoke(None, config)` — returned without running `end_trip`. Reproduced
locally: with the old key in the checkpoint the resume is a no-op; without it, it runs.
Why LangGraph skips the pending node in that case was not traced inside the library.

The handler then trusted the resume: it sent the fallback "Trip ended." and cleared the
conversation, while the trip and its expenses were untouched.

## Decision

- **Thread IDs carry a schema version**: `thread_id_for(ledger_id)` returns
  `"<ledger>:v<THREAD_SCHEMA_VERSION>"`. Bump `THREAD_SCHEMA_VERSION` in `graph.py`
  whenever `AgentState`'s fields change, so new code never resumes a thread written under
  the old shape. Old threads expire through the checkpointer's 90-day TTL.
- **`handle_callback` checks the trip ended** after the resume, by reading `TRIP#ACTIVE`.
  If it still exists, the handler logs an error, tells the user nothing was deleted and
  to send "end trip" again, sends no charts or CSV, and clears the thread — which is
  otherwise left paused at a confirmation whose keyboard is gone, refusing every message.

## Consequences

- A version bump resets every chat's conversation context once. Expenses, trips and
  archives live outside the thread and are unaffected.
- Forgetting a bump is now a visible failure — "couldn't end the trip" — rather than a
  silent false success.

## Alternatives considered

- **Clear all threads manually on any deploy that changes the state.** No code, but a
  step that is easy to forget, and forgetting it reproduces this failure.
- **Migrate old checkpoints.** Rewriting another library's storage format to save
  conversation context that is cheap to lose.
