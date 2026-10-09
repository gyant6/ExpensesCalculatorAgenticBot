# 0014. Structured JSON logs, with a record of every conversation turn

- **Status:** Accepted
- **Date:** 2026-10-09

## Context

The conversation checkpoint is deleted when a trip ends
([0013](0013-bounded-history-and-once-per-turn-checkpoints.md)), and the logs held only
timings and ids. Once a trip was over, nothing in AWS showed what users had said or what
the bot had done. Diagnosing the September trip's wrong-row edits meant reading the
group's Telegram history through a browser, and test scenarios have no realistic source.
The logs were also plain text, so Logs Insights could not filter on any field without a
`parse` step per query.

## Decision

- **Every log line is a JSON object** (`src/bot/logging_setup.py`), with `timestamp`,
  `level`, `logger`, `message`, the Lambda request id, any exception, and fields
  attached through `extra=log_fields(...)`. The bot token and webhook secret are
  replaced in the finished line, as the previous formatter did for the token.
- **Every turn writes one record** (`src/bot/turn_log.py`, `event = "turn"`): chat,
  sender, `update_id`, the user's message, each tool call with its arguments and result
  (cut at 8,000 characters), the reply, model calls and tokens, timings and the outcome.
  It replaces the per-turn timing line. A turn that raises is recorded, with the error,
  before the exception propagates.
- **Not logged:** messages the bot ignores (addressed to someone else), and anything
  before authorisation. Those are not the bot's conversations.
- **Retention stays at the log group's 60 days.** The owner decides what is kept; group
  members' preferences are not a constraint on this deployment.

## Consequences

- A past conversation can be read with one Logs Insights query (RUNBOOK, "Reading a
  conversation from the logs"), from the CLI, without Telegram.
- Message content now sits in CloudWatch for 60 days. CloudWatch cannot delete single
  events: a record leaves only when the retention expires, or with the whole log stream.
- No change to Lambda's log format setting is needed: the application's formatter
  writes the JSON, and Logs Insights discovers fields in JSON lines. Lambda's own START,
  END and REPORT lines stay plain text.
- Local polling also logs JSON, so one format is tested and run everywhere.

## Alternatives considered

- **A JSON turn record beside plain-text logs.** Smallest change, but every other line
  — timings, errors — would still need parsing to query.
- **Turn records in DynamoDB** with a TTL. Queryable by chat, and deletable per chat on
  request, but a write on every message, and conversation history in the table that
  holds the ledger. Per-chat deletion was not a requirement.
- **LangSmith tracing.** Purpose-built, with datasets built from traces, but it sends
  every conversation to a third party; it remains on the roadmap for evaluation work.
