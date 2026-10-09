# 0013. Bound the history sent to the model; save the conversation once per turn

- **Status:** Accepted
- **Date:** 2026-10-09

## Context

Measured on a September 2026 trip: 297 model calls averaging ~34K input tokens, peaking
at 90K, because every call resent the whole trip's conversation — over 99% of the bill,
and most of the latency (one "show all" spent 26 s of 27.5 s in the model). By late in
the trip the model was also reading dozens of old numbered lists.

Separately, the checkpointer saved a full snapshot after **every graph step** and kept
them all. Measured against moto: one message with a tool call wrote 7 snapshots and 41
chunk and write items; a plain "hi" in production left 26. Ending a long trip spent
14.8 s deleting them.

## Decision

- **History sent to the model is bounded.** `bounded_history` passes roughly the last
  `MODEL_HISTORY_TOKEN_BUDGET` (8,000) tokens, counted approximately, cut only at the
  start of a user message so a tool call is never separated from its result. The
  current turn is always sent whole. The checkpoint keeps the full history; only what is
  sent is cut. The tools read expenses live, so older turns are context, not data.
- **The conversation is saved once per turn.** Every `graph.invoke` passes
  `durability=CHECKPOINT_DURABILITY` (`"exit"`): one snapshot when the run finishes or
  pauses at the end-trip confirmation. The confirmation resumes from it — tested on the
  real checkpointer.

## Consequences

- One message with a tool call writes 1 snapshot and 1 item instead of 7 and 41.
- A crash mid-turn loses that turn's conversation. Expenses its tools already wrote are
  kept; with redeliveries dropped ([0008](0008-redelivered-updates-dropped-by-update-id.md)),
  nothing would have resumed the turn anyway.
- One snapshot per turn still accumulates during a trip; `clear_thread_history` and the
  90-day TTL remove them.
- Context older than the budget is not seen by the model. A reference to something said
  long ago fails visibly ("which one?"), and the expense data itself is always current.
- Prompt caching is deferred to the Haiku 5.5 move: the fixed prefix (~3.3K tokens) is
  below Haiku 4.5's 4,096-token minimum.

## Alternatives considered

- **Prune old snapshots after each turn** with `DynamoDBSaver.prune(...,
  "keep_latest")`. The method exists but is inherited unimplemented in
  langgraph-checkpoint-aws 1.1.1 and raises `NotImplementedError`; deployed, every reply
  would have been followed by an error. Writing our own deletes against the library's
  storage format would be fragile.
- **Trim the stored history** (delete old messages from the checkpoint). Smaller
  snapshots, but irreversible, and edits to a thread paused at the confirmation are the
  kind of state change that broke a resume once ([0012](0012-versioned-conversation-threads.md)).
- **Summarise older turns** with an extra model call. More context kept, at the cost of a
  call per summary and another failure mode, for data the tools already read live.
