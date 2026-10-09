# Architecture: Overseas Expenses Telegram Bot

## Overview

An agentic Telegram chatbot — *Zuzu* — that helps users track overseas travel expenses. The agent uses LangGraph for stateful conversation management, Claude Haiku on AWS Bedrock as the LLM, and DynamoDB as the sole database (conversation history + expenses).

---

## Tech Stack

| Concern | Choice | Reason |
|---|---|---|
| LLM | Claude Haiku (`global.anthropic.claude-haiku-4-5-20251001-v1:0`) on Bedrock | Fast responses, low cost, sufficient for structured extraction |
| Agent framework | LangGraph | Production-standard stateful agent; checkpointing built-in; CV-worthy |
| LLM integration | `langchain-aws` (`ChatBedrockConverse`) | First-class LangChain/LangGraph integration with Bedrock |
| Telegram | `python-telegram-bot` | Well-maintained, supports both polling (local) and webhook (Lambda) |
| Database | DynamoDB (single-table) | Native CRUD, serverless, free tier sufficient |
| Local DB | DynamoDB Local (Docker) | Identical boto3 API; switch via `DYNAMODB_ENDPOINT_URL` env var |
| Conversation state | LangGraph DynamoDB checkpointer (`langgraph-checkpoint-aws`, `DynamoDBSaver`) | Persists full graph state per user; delete checkpoint = clear history |
| Packaging | `uv` + `pyproject.toml` | Modern Python standard; fast installs; clean Lambda packaging |
| Charts | `matplotlib`, in a dedicated Lambda | Generate pie chart (by category) and bar chart (by day) as PNGs in memory; sent as Telegram photos on trip end. Isolated in its own function so the main one never imports matplotlib or numpy |
| CSV export | Python stdlib `csv` | Generate expense CSV with SGD-equivalent column on trip end; sent as Telegram file attachment. Kept free of matplotlib so `end_trip` can build it in the main function |
| FX rates | `api.fxratesapi.com` | Free, no auth required, simple GET |

---

## Repository Layout

```
ExpensesCalculatorAgenticBot/
├── pyproject.toml
├── .env.example
├── .env                          # gitignored
├── docker-compose.yml            # DynamoDB Local + one-off table creation
├── dev_runner.py                 # interactive terminal REPL against the graph, no Telegram
├── src/
│   ├── __init__.py
│   └── bot/
│       ├── __init__.py
│       ├── main.py               # entrypoint (polling locally, Lambda handler in prod)
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── graph.py          # LangGraph graph definition + router
│       │   ├── nodes.py          # check_trip_status, agent_node, LLM + tool binding
│       │   ├── state.py          # AgentState TypedDict
│       │   └── prompts.py        # system prompt
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── trip.py           # start_trip, end_trip
│       │   ├── expenses.py       # add, edit, delete, list expenses
│       │   └── fx.py             # get_sgd_exchange_rates
│       ├── storage/
│       │   ├── __init__.py
│       │   └── dynamodb.py       # DynamoDB client + table operations
│       ├── charts.py             # pie and bar chart rendering (matplotlib; chart Lambda only)
│       ├── chart_handler.py      # chart Lambda entrypoint: payload in, base64 PNGs out
│       ├── charts_client.py      # invokes the chart Lambda; renders in-process locally
│       ├── chart_protocol.py     # payload keys shared by both functions; no dependencies
│       ├── export.py             # CSV generation and SGD conversion (no matplotlib)
│       ├── auth.py               # AuthStatus, EntityType, AuthCommand, AUTH# key constants
│       ├── telegram_handler.py   # receives Telegram updates, calls agent
│       └── config.py             # settings via pydantic-settings
└── tests/
    ├── __init__.py
    ├── conftest.py               # moto fixtures + table creation
    ├── unit/
    │   ├── __init__.py
    │   ├── test_config.py
    │   ├── test_export.py
    │   ├── test_charts.py
    │   ├── test_chart_handler.py
    │   ├── test_charts_client.py
    │   ├── test_chart_contract.py
    │   ├── test_auth.py
    │   ├── test_graph.py
    │   ├── test_prompts.py
    │   ├── test_telegram_handler.py
    │   ├── tools/
    │   │   ├── __init__.py
    │   │   ├── test_trip.py
    │   │   ├── test_expenses.py
    │   │   └── test_fx.py
    │   └── storage/
    │       ├── __init__.py
    │       └── test_dynamodb.py
    ├── integration/
    │   ├── __init__.py
    │   ├── conftest.py           # DynamoDB Local fixtures
    │   └── test_full_flow.py
    └── evals/
        ├── __init__.py
        ├── datasets/
        │   ├── expense_extraction.json       # 20+ labelled examples
        │   ├── intent_classification.json
        │   └── end_trip_confirmation.json    # multi-turn examples: confirmation vs denial vs ambiguous
        ├── evaluators.py         # LangSmith evaluator definitions
        └── run_evals.py          # entrypoint: uv run python -m tests.evals.run_evals
```

---

## Architecture Diagrams

### Local Development

```
User (Telegram app)
        │
        ▼
  Telegram Servers
        │  (polling — bot pulls updates every second)
        ▼
  python-telegram-bot (polling)
        │
        ▼
  telegram_handler.py
        │  (thread_id_for(ledger_id) as thread_id)
        ▼
  LangGraph Agent (graph.py)
        │
        ├──► DynamoDB Local (localhost:8000, Docker)
        │         ├── Conversation checkpoints
        │         └── Trip + Expense items
        │
        ├──► AWS Bedrock
        │         └── Claude Haiku (via local AWS credentials)
        │
        └──► api.fxratesapi.com (HTTPS)
```

### AWS Production (Phase 2)

```
User (Telegram app)
        │
        ▼
  Telegram Servers
        │  (webhook POST)
        ▼
  API Gateway (POST /webhook)
        │
        ▼
  Lambda Function (main)
        │
        ├──► DynamoDB (real, same table schema)
        │         ├── Conversation checkpoints (langgraph-checkpoint-aws)
        │         └── Trip + Expense items
        ├──► Bedrock (Claude Haiku, same region)
        ├──► api.fxratesapi.com
        │
        └──► Lambda Function (charts)      [trip end only, synchronous invoke]
                  expenses + FX rates in, two PNGs out.
                  Reads no database and holds no state.
```

The chart function exists so matplotlib and numpy stay out of the main function, which
would otherwise import them on every cold start — including one that only records an
expense. It is invoked once per trip end and never on an ordinary message.

Locally the charts are rendered in-process instead of invoked, since matplotlib is
installed in the dev environment. That branch is on `ENVIRONMENT`, the same switch that
decides whether secrets come from SSM. Beyond those two branches the code is identical
between local and prod — no business logic changes.

---

## Authentication

Two independent layers, both required in production.

### Layer A — Webhook origin verification (API Gateway, prod only)

Defense-in-depth: both controls must be in place.

**1. Resource policy — IP allowlist**
API Gateway resource policy restricts inbound requests to Telegram's published server IP ranges ([cidr.txt](https://core.telegram.org/resources/cidr.txt)). Requests from any other IP are rejected at the gateway before Lambda is invoked — zero Lambda cost for non-Telegram traffic. The CIDR list must be kept in sync with Telegram's published ranges whenever they change.

**2. Webhook secret token — header validation**
When registering the webhook with Telegram (`setWebhook`), set the `secret_token` parameter. Telegram includes `X-Telegram-Bot-Api-Secret-Token: <your_secret>` on every webhook POST. The Lambda handler validates this header before processing the request. This proves the request is a legitimate webhook from your specific bot, not just any traffic originating from a Telegram IP.

IP allowlist alone is insufficient because it does not prove the request is for your bot. Secret token alone is insufficient because a stolen token could be replayed from any IP. Together they provide defence in depth.

The secret token is stored in AWS SSM Parameter Store (SecureString) and loaded via `config.py` at Lambda startup.

**Keeping the IP allowlist in sync — automated CIDR updater**

Telegram's published IP ranges change over time. The allowlist is kept current by a dedicated Lambda triggered by an EventBridge scheduled rule (weekly cadence — more responsive than monthly while remaining essentially free).

```
EventBridge (weekly schedule)
        │
        ▼
cidr-updater Lambda
        │
        ├── GET https://core.telegram.org/resources/cidr.txt
        │         (parse IPv4 + IPv6 CIDR ranges)
        │
        └── apigateway:UpdateRestApiPolicy
                  (replace resource policy on the webhook API)
```

Design constraints:
- **Failure safety:** if the GET fails or returns unparseable content, the Lambda raises an exception and leaves the existing policy unchanged — it never partially updates.
- **Resource policy size limit:** API Gateway resource policies have a documented size limit (check AWS docs before deploying — Telegram's CIDR list has been growing).
- **IAM scope:** the Lambda execution role grants `apigateway:UpdateRestApiPolicy` scoped to the specific webhook API ARN only.
- **Structured logging:** logs the full new policy document on every successful update so changes are auditable in CloudWatch.

### Layer B — Access control (telegram_handler.py, local and prod)

`telegram_handler.py` checks the incoming `user_id` (or `chat_id` for groups) against `AUTH#<id>` items in DynamoDB before invoking the agent. This runs first, before any Bedrock or expense-related DynamoDB calls.

**DynamoDB schema for auth items:**

| PK | SK | Attributes |
|---|---|---|
| `AUTH#<id>` | `PROFILE` | `status` (PENDING/APPROVED/REJECTED), `entity_type` (USER/GROUP), `username`, `requested_at`, `reviewed_at` |

Telegram user IDs are positive integers; group IDs are negative — the same `AUTH#<id>` key scheme covers both. The `entity_type` field makes the distinction explicit for querying and display.

**Auth check logic (on every incoming message):**

1. Determine scope: private chat → use `user_id`; group chat → use `chat_id` (negative).
2. Look up `AUTH#<id>` for that scope only. Private and group approvals are independent — a user approved in a group is not approved for DMs, and vice versa.
3. `APPROVED` → proceed normally.
4. `PENDING` → reply "Your access request is still pending approval." Do nothing else.
5. `REJECTED` → silently ignore.
6. Not found → create `AUTH#<id>` with `status=PENDING`, then send an approval request to the admin.

**Approval request sent to admin (Telegram ID `111111111`):**

```python
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("Approve", callback_data=f"auth:approve:{auth_id}")],
    [InlineKeyboardButton("Reject",  callback_data=f"auth:reject:{auth_id}")],
])
message = f"Access request from {display_name} ({entity_type} ID: {auth_id})"
```

Callback prefix `auth:` is handled by a dedicated `CallbackQueryHandler` in `main.py`, parallel to the existing `end_trip:` handler. On Approve/Reject, the handler updates `AUTH#<id>.status` and `reviewed_at` in DynamoDB and notifies the requester.

**Group extension:** Group and user approvals are independent scopes — being approved in a group does not grant direct message access, and vice versa.

- Private chat → check `AUTH#<user_id>` only.
- Group chat → check `AUTH#<chat_id>` (negative group ID) only.

Approving a group ID grants access to all members messaging the bot within that group. It does not affect whether those members can message the bot directly.

**Admin ID** is stored in config as `ADMIN_TELEGRAM_ID` (env var / SSM in prod), not hardcoded.

---

## LangGraph Agent Design

### Graph Structure

```
START
  │
  ▼
[check_trip_status] ◄──────────────────────────────────────────┐
  │  (reads TRIP#ACTIVE; sets trip_start_date, trip_timezone    │
  │   and local_date — message_time as a day in the trip zone)  │
  ▼                                                            │
[agent_node] ◄─────────────────────────────────────────┐       │
  │                                                    │       │
  │  custom_routes(state) inspects the last message:   │       │
  │                                                    │       │
  ├── not an AIMessage, or no tool_calls ──► END        │       │
  │                                                    │       │
  ├── end_trip is the only tool call                   │       │
  │        └──► [end_trip_node] ───────────────────────┤       │
  │               interrupt_before: graph pauses here   │       │
  │               before the tool executes, persists    │       │
  │               state, and returns to the handler     │       │
  │                                                    │       │
  ├── end_trip batched with other tools                │       │
  │        └──► [end_trip_batch_error_node] ───────────┘       │
  │               injects a rejecting ToolMessage for          │
  │               every call in the batch, so the model        │
  │               retries with end_trip alone                  │
  │                                                            │
  └── only non-end_trip tools                                  │
           └──► [tools_node] ──────────────────────────────────┘
                  start_trip, set_trip_timezone, add_expense,
                  edit_expense, delete_expense, get_all_expenses
```

The batch case exists because routing a mixed batch either way loses a call silently:
`tools_node` has no `end_trip` bound, and `end_trip_node` has none of the others. Worse,
`tools_node` is not interrupted, so a batched `end_trip` would skip the confirmation
entirely. Rejecting the whole batch is the only option that neither drops a tool call nor
deletes a trip without asking.

This is a standard ReAct loop implemented as a LangGraph graph. `agent_node` calls Claude Haiku with the bound tools. `custom_routes` then inspects the last message: anything that is not an `AIMessage` carrying tool calls ends the turn, an `end_trip` call on its own routes to `end_trip_node`, `end_trip` mixed with other tools routes to `end_trip_batch_error_node`, and any other tool call routes to `tools_node`. `tools_node` edges back through `check_trip_status`, so a trip started or moved to another time zone earlier in the turn — "start trip in Seoul, coffee 5" — is re-read, with its local date, before the model records the coffee; the other two tool nodes edge straight back to `agent_node`. The loop continues until Claude returns a plain message.

`end_trip` sits in its own node so that `interrupt_before=["end_trip_node"]` pauses that one tool without interrupting any of the others. This provides one structural guarantee: `end_trip` can never execute on the same turn the LLM first decides to call it. When the LLM emits an `end_trip` tool call, the graph pauses before the node runs, persists state to the checkpointer, and returns. `handle_message` detects the interrupted state via `graph.get_state(config).next` (non-empty when interrupted) and sends a Yes/No inline keyboard.

Confirmation arrives as a callback query rather than as a new text message. `handle_callback` first renders the chart and CSV attachments from the still-live expense data, then resumes with `graph.invoke(None, config)` so `end_trip_node` genuinely executes: the tool performs the export and the deletion and returns the CSV that `agent_node` turns into a summary. Once that summary and the attachments have been delivered, the handler calls `clear_thread_history` to delete the thread's checkpoints. While the graph is interrupted, `handle_message` declines new text messages and asks the user to confirm or cancel first.

### State Definition

```python
# src/bot/agent/state.py
from langgraph.graph import MessagesState

class AgentState(MessagesState):
    # MessagesState provides: messages: list[BaseMessage]
    # thread_id is managed by the checkpointer config, not state
    ledger_id: str              # Whose expenses these are — the chat, not the sender. Set by
                                # telegram_handler.py on every invocation; injected into tools
                                # via InjectedState so the LLM never sees or supplies it
    message_time: str           # The incoming message's own timestamp, UTC ISO-8601; set by
                                # telegram_handler.py. Its own time, not the processing time,
                                # so a delayed or redelivered update keeps its day
    trip_start_date: str | None # Set by check_trip_status; tells the LLM whether a trip is
                                # active and when it started
    trip_timezone: str          # Set by check_trip_status: the trip's IANA zone, or
                                # Asia/Singapore when no trip is active
    local_date: str             # Set by check_trip_status: message_time as a YYYY-MM-DD day in
                                # trip_timezone. Given to the LLM as "Today is …" and used by
                                # add_expense as the default date — the same value for both
```

### Checkpointing (Conversation Memory)

- `thread_id` = `thread_id_for(ledger_id)` = `"<ledger_id>:v<THREAD_SCHEMA_VERSION>"`. Bump the version whenever `AgentState` changes, so a thread written under the old shape is never resumed ([0012](docs/decisions/0012-versioned-conversation-threads.md))
- LangGraph checkpointer (`langgraph-checkpoint-aws`, `DynamoDBSaver`) persists the full `messages` list to DynamoDB once per turn: every `graph.invoke` passes `durability=CHECKPOINT_DURABILITY` (`"exit"`), which saves when the run finishes or pauses at the end-trip confirmation, not after every step
- The model sees only the last ~`MODEL_HISTORY_TOKEN_BUDGET` tokens of that history (`bounded_history`, cut at the start of a user message); the checkpoint keeps it all ([0013](docs/decisions/0013-bounded-history-and-once-per-turn-checkpoints.md))
- When a trip ends: the `end_trip` tool deletes all expense and trip items, then the caller (`telegram_handler` or `dev_runner`) calls `clear_thread_history` to delete every checkpoint for this `thread_id`. `handle_callback` first checks `TRIP#ACTIVE` is gone; if the resume left the trip active, it reports the failure instead of a summary, sends no files, and clears the thread so the next "end trip" starts over. The deletion cannot live in the tool — `agent_node` writes the summary after the tool returns, using the very history being deleted, so it runs only once the summary and attachments have been delivered
- When a trip starts: a fresh checkpoint begins automatically on the next message

---

## Group Message Filtering

Privacy mode is **disabled** on this bot (`can_read_all_group_messages: true`), so Telegram
delivers every group message, not only those mentioning it. That is what allows a plain
`12 dollars for lunch` to be recorded without anyone having to address the bot — but it
also means a conversation between two members would otherwise reach the model, costing a
Bedrock call and sometimes drawing an unwanted reply.

`_addressed_to_someone_else` drops a message that mentions somebody but never the bot. It
runs before the auth gate, so an ignored message costs neither a DynamoDB read nor an
access request.

**It applies in private chats too**, which is a deliberate trade rather than an oversight.
In a group a mention is an address; in a DM it is more often descriptive — `lunch with
@bob $12` is an expense, not a message to Bob — and that message is now dropped.
Naming the bot anywhere overrides it, so `@ZuzuAssistantBot lunch with @bob $12` is
recorded. Restricting the filter to groups is a one-line change if the DM behaviour proves
more annoying than useful.

Two details that are easy to get wrong:

- **Read Telegram's entities, not the text.** Searching for `@` also matches an email
  address; Telegram classifies that as an `EMAIL` entity and a real tag as `MENTION` or —
  for a user with no username — `TEXT_MENTION`, which carries the user object instead.
- **Extract with `parse_entity`.** Telegram counts offsets in UTF-16 code units, so an
  emoji occupies two while Python sees one. Slicing the string directly starts a character
  late, and the bot reads its own name as somebody else's — meaning a message addressed to
  it gets ignored. This surfaces only when the mention is of the bot, since misreading any
  other name still yields the right answer by luck.

Mentioning the bot alongside others is still addressing it, so those messages are handled.

---

## Ledger Scope

A **ledger** is the thing expenses belong to. In a private chat that is the user; in a
group it is the group, so everyone contributes to one trip and sees one list. Telegram
gives a private chat the same ID as its user, so `ledger_id` is just the chat ID in both
cases and `_ledger_id_for` needs no branch.

This matches the auth model, which already approves a group as a single entity — a member
messaging through an approved group has no `AUTH#` record of their own. Storage originally
disagreed: it keyed on the sender, so each member of a group got a private trip inside a
shared conversation and neither could see the other's expenses. Authorisation said "this
group is one thing"; storage said "these people are separate". They now agree.

Consequences worth knowing:

- The same person has **two independent ledgers** — their private chat and each group they
  use the bot in. That is intended; a personal trip is not a group trip.
- The checkpoint `thread_id` is the ledger too, so a group shares one conversation. "Show
  my expenses" from any member sees the same list, and context carries across members.
- Expenses currently record **no author**. In a shared ledger "who added the carrot" is a
  real question the data cannot answer, since `source_message` keeps the text but not who
  wrote it. Worth adding before using a group ledger to split costs.
- The DynamoDB partition key is still `USER#<ledger_id>`, which reads oddly for a group.
  Renaming the prefix would be a data migration, so it is a deliberate deferral rather
  than an oversight.

---

## DynamoDB Table Design (Single-Table)

**Table name:** `ExpensesCalculator` (configurable via `DYNAMODB_TABLE_NAME`)

**Primary key:** `PK` (String) + `SK` (String)

| PK | SK | Attributes | Description |
|---|---|---|---|
| `USER#<ledger_id>` | `TRIP#ACTIVE` | `start_date`, `timezone` | Active trip marker; `timezone` is the trip's IANA zone, absent on trips started before zones existed |
| `USER#<ledger_id>` | `EXPENSE#<id>` | see below | Individual expense, keyed by an immutable 4-character id |
| `USER#<ledger_id>` | `ARCHIVE#<ended_at>` | `start_date`, `ended_at`, `expenses`, `ttl` | An ended trip's expenses, kept for `TRIP_ARCHIVE_TTL_SECONDS` (90 days) |
| `UPDATE#<update_id>` | `MARKER` | `claimed_at`, `ttl` | A Telegram update already handled; a redelivery finding it is dropped. Expires after `UPDATE_DEDUP_TTL_SECONDS` (one day) |
| `AUTH#<id>` | `PROFILE` | `status`, `entity_type`, `username`, `requested_at`, `reviewed_at` | Access control record (user or group) |

`<id>` is the Telegram user ID (positive) or group ID (negative). `entity_type` is `USER` or `GROUP`. `status` is `PENDING`, `APPROVED`, or `REJECTED`.

**Expense item attributes:**

| Attribute | Type | Example |
|---|---|---|
| `PK` | String | `USER#123456789` |
| `SK` | String | `EXPENSE#k7qm` |
| `date` | String (ISO-8601) | `2026-06-04` |
| `source_message` | String | `1200 yen at Ichiran ramen for dinner` |
| `category` | String | `Food` |
| `currency` | String | `JPY` |
| `amount` | Number (Decimal) | `1200` |
| `summary` | String | `Dinner at Ichiran ramen` |
| `payment_method` | String | `Card` |
| `timezone` | String (IANA) | `America/Los_Angeles` — the zone `date` is a calendar day in |
| `created_at` | String (ISO-8601, UTC) | `2026-06-04T13:45:00.000000+00:00` |
| `updated_at` | String (ISO-8601, UTC) | `2026-06-04T13:45:00.000000+00:00` |

**Trip archive item.** `end_trip` copies the trip into a single item before deleting
anything, so ending a trip by mistake, or losing the CSV sent to the chat, is
recoverable. `expenses` is a list holding every expense item in full, minus `PK`. `ttl`
is epoch seconds; the table's TTL setting names that attribute, and DynamoDB deletes the
item some time after it passes — typically within days, not at the second. Each archive
carries its own `ttl`, so retention could differ per trip later without a schema change.
One item rather than one per expense, so the archive is one put that lands whole or not
at all; DynamoDB's 400 KB item limit caps a trip at roughly a thousand expenses, beyond
which the put fails and nothing is deleted. Nothing in the bot reads archives yet:
restoring one is done by hand with the AWS CLI.

**Local vs prod switch:** Set `DYNAMODB_ENDPOINT_URL=http://localhost:8000` in local `.env`. Unset (or absent) in prod — boto3 connects to real DynamoDB automatically.

---

## Tools

All tools are LangChain `@tool`-decorated functions. `ledger_id`, `message_time`, `local_date` and `trip_timezone` are injected from `AgentState` by the LangGraph tool node — the LLM never sees them as parameters. `telegram_handler.py` sets `ledger_id` and `message_time` before invoking the graph; `check_trip_status` derives the other two.

### 1. `start_trip`
- **Input:** `timezone: str` — the IANA zone of where the user is travelling. The model maps the place ("Japan" → `Asia/Tokyo`) and, if the user named none, asks once before calling.
- **Action:** Returns an error if `TRIP#ACTIVE` exists (only one active trip) or `zoneinfo` does not recognise the zone. Otherwise writes `TRIP#ACTIVE` with `timezone` and `start_date` — `message_time` as a day in that zone.
- **Returns:** Confirmation with the start date and zone.

### 1a. `set_trip_timezone`
- **Input:** `timezone: str` — the zone the user has moved to ("I'm in Seoul now").
- **Action:** Validates the zone, refuses if no trip is active, and updates `TRIP#ACTIVE.timezone`. Expenses already recorded keep their dates; new ones, and the "Today is" line from the next step on, follow the new zone.
- **Returns:** Confirmation with today's date in the new zone.

### 2. `add_expense`
- **Input:** `source_message: str`, `summary: str`, `category: str`, `amount: str`, `currency: str`, `date: str | None = None`, `payment_method: str = "Card"`
- **Action:** Writes an `EXPENSE#<id>` item, where `<id>` is four random characters from an alphabet without look-alikes (`23456789abcdefghjkmnpqrstuvwxyz`). The write is conditional on the key being free, so a collision retries with a fresh id rather than overwriting; five consecutive collisions raise. Records `created_at` and `updated_at` in UTC, and the trip's `timezone`. No FX conversion at write time. When `date` is None, uses `local_date` — today in the trip's zone, the date the model was told.
- **Returns:** `"Expense recorded with id <id>."` — the model keeps the id for a follow-up edit in the same conversation and never shows it — or a validation error string.
- **Note:** The LLM extracts all structured fields from the user's raw message. If the user does not mention a currency, the LLM defaults `currency` to `"SGD"`. `category` must be one of the values in `CATEGORIES`; `amount` must parse as a positive `Decimal`; `date` must be `YYYY-MM-DD`.

### 3. `edit_expense`
- **Input:** `expense_id: str`, `expected_amount: str`, `edit_message: str`, and any subset of `summary`, `category`, `amount`, `currency`, `date`, `payment_method`
- **Action:** Fetches `EXPENSE#<expense_id>` with one `get_item`, and refuses unless its current amount equals `expected_amount` (compared as `Decimal`). Then updates the supplied fields and `updated_at` in place, appending `edit_message` to `source_message`. A date edit is an ordinary update — the key is the id, so nothing moves. `summary` changes only when passed, which the model does only when the user asks to rename.
- **Returns:** `"Edit expense successful."`, or an error string if nothing was to change, the id is unknown, the amount did not match (naming what the id actually holds), or a value failed validation.

### 4. `delete_expense`
- **Input:** `expense_id: str`, `expected_amount: str`
- **Action:** The same fetch and amount check as `edit_expense`, then deletes the item.
- **Returns:** `"Expense deleted."`, or an error string if the id is unknown or the amount did not match.

### 5. `get_all_expenses`
- **Input:** _(none beyond ledger_id)_
- **Action:** `list_expenses`: queries all `EXPENSE#*` items for the ledger and sorts them by `date`, then `created_at`. The end-trip CSV and attachments use the same function, so every list reads chronologically.
- **Returns:** `number | id | summary | category | amount currency | date | payment_method`, one line per expense, or a message indicating none are recorded. The id column is for the model's edits and deletes only; the system prompt tells it to show the user numbered lines without ids.

### 6. `end_trip`
- **Input:** _(none beyond user_id)_
- **Human-in-the-loop:** The graph is compiled with `interrupt_before=["end_trip_node"]`. This guarantees `end_trip` never executes on the same turn the LLM first decides to call it. The graph pauses, saves state to the checkpointer, and returns control to `handle_message`, which sends a Yes/No inline keyboard to the user.
- **Action (once the node runs, after confirmation):**
  1. Returns an error and deletes nothing if no `TRIP#ACTIVE` item exists.
  2. Queries all `EXPENSE#*` items for the user.
  3. Calls `get_sgd_exchange_rates()`. On failure it continues without rates rather than blocking the trip from ending.
  4. Builds the CSV via `generate_csv(expenses, fx_rates)` — before any deletion, so a failed export leaves the trip intact rather than destroying records with no copy of them.
  5. Writes the `ARCHIVE#<ended_at>` item holding every expense, with a `ttl` 90 days out. Skipped for a trip with no expenses. If this write fails, the tool fails and nothing is deleted.
  6. Deletes every `EXPENSE#*` item and the `TRIP#ACTIVE` item.
- **Returns (to LLM):** A confirmation line followed by the CSV of all expenses, including the `amount_sgd` column, so the summary is written from real figures. If rates were unavailable, `amount_sgd` is blank and the CSV is prefixed with an instruction to give per-currency totals and state no SGD total — without that instruction the model invents an exchange rate to satisfy the system prompt's request for one.
- **On confirm (`handle_callback`):**
  1. Renders the charts and the CSV attachment from the live expense data. This must precede the resume, because the tool deletes that data.
  2. Resumes the graph with `graph.invoke(None, config)`, which runs the node described above.
  3. Sends the agent's summary text, then the two charts and `expenses.csv`.
  4. Calls `clear_thread_history` to delete the thread's checkpoints.
- **Sent to user:** LLM summary text → pie chart photo → bar chart photo → `expenses.csv` file attachment.

### 7. `get_sgd_exchange_rates`
- **Input:** _(none)_
- **Action:** `GET https://api.fxratesapi.com/latest?base=SGD`. Fetches all rates with SGD as the base.
- **Returns:** `dict` mapping currency codes to their rate relative to SGD (e.g. `{"JPY": 167.5, "USD": 0.74}`). To convert a foreign amount to SGD: `sgd_amount = foreign_amount / rates[currency]`.
- **Note:** Synchronous, because its caller `end_trip` is a sync tool executing inside `graph.invoke` and cannot await. Called twice per trip end — once inside `end_trip` for the CSV, once in `_render_attachments` for the charts. The rates fetched by the handler are passed to the chart function in the invoke payload, so the charts and the CSV are never drawn from separately fetched rates. Not bound to the LLM.

---

## Expense Parsing Flow (AI-Assisted Structured Extraction)

The LLM extracts structured fields from the user's natural language before calling `add_expense`. This is handled inside the agent loop — the model is prompted to identify these fields before invoking the tool:

```
Example A — foreign currency explicitly mentioned:
  User: "spent 1200 yen at Ichiran ramen for dinner yesterday"
  LLM extracts: date=2026-06-03, amount=1200, currency=JPY,
                category=Food, summary="Dinner at Ichiran ramen"
  Stored as-is: amount=1200, currency=JPY (no FX call at write time)

Example B — no currency mentioned, defaults to SGD:
  User: "paid $12 for chicken rice at Maxwell"
  LLM extracts: date=2026-06-04, amount=12, currency=SGD,
                category=Food, summary="Chicken rice at Maxwell"
  Stored as-is: amount=12, currency=SGD

FX conversion happens at end_trip, never at write time:
  get_sgd_exchange_rates() → {"JPY": 167.5, ...}
  JPY expense: sgd_amount = 1200 / 167.5 = 7.16
  SGD expense: sgd_amount = 12 (no conversion)

Rates are fetched twice per trip end — once inside end_trip for the CSV handed to the
LLM, once in the handler for the charts. Both call the same function, so the figures
cannot diverge in logic, only across the seconds between the two HTTP calls.
```

---

## Trip Summary (end_trip output)

Sent to the user as four Telegram messages in sequence:

**Message 1 — Text (plain text):**
LLM-generated warm summary (2–3 sentences) including total SGD spend, followed by a
per-category SGD breakdown, one line per category:
```
What a trip! You spent a total of SGD 58.82 across 7 expenses over 4 days.

Food: SGD 32.10
Transport: SGD 15.44
Leisure: SGD 11.28
```

**Message 2 — Photo:** Pie chart of spending by category (PNG, rendered in memory by the chart Lambda; in-process locally).

**Message 3 — Photo:** Bar chart of daily spending in SGD (PNG, same path as above).

Both photos are omitted if rendering fails, or if FX rates were unavailable — the charts plot SGD only. The text summary and the CSV are always sent.

**Message 4 — File:** `expenses.csv` with columns: `date, summary, category, amount, currency, amount_sgd, payment_method`.

---

## Runbook

Operational procedures — deploying, rotating secrets and the webhook URL, diagnosing a
silent bot, and restoring data or Terraform state — are in [RUNBOOK.md](RUNBOOK.md).

## Environment Configuration

```bash
# .env.example

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here

# AWS
AWS_REGION=ap-southeast-2
AWS_BEDROCK_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0
AWS_ACCESS_KEY_ID=          # local: from ~/.aws/credentials; Lambda: IAM role
AWS_SECRET_ACCESS_KEY=      # local: from ~/.aws/credentials; Lambda: IAM role

# DynamoDB
DYNAMODB_TABLE_NAME=ExpensesCalculator
DYNAMODB_ENDPOINT_URL=http://localhost:8000   # remove this line in prod

# App
LOG_LEVEL=INFO
ENVIRONMENT=local   # or: production
ADMIN_TELEGRAM_ID=   # Telegram user ID that receives access-request notifications

# Charts — production only. Locally the charts are rendered in-process, so both are
# unused and CHART_LAMBDA_FUNCTION_NAME may be omitted entirely.
CHART_LAMBDA_FUNCTION_NAME=   # name of the deployed chart Lambda
CHART_LAMBDA_TIMEOUT_SECONDS=30

# LangSmith (evals only — not required for the bot to run)
LANGSMITH_API_KEY=your_langsmith_api_key_here
LANGSMITH_PROJECT=expenses-bot
```

---

## Testing Strategy

### Philosophy

Agentic AI applications have two distinct testing concerns:

1. **Deterministic code** (tools, storage, config) — standard unit and integration tests. These should have high coverage and be fast.
2. **Non-deterministic LLM behaviour** (intent classification, field extraction, response quality) — cannot use `assert output == expected`. Instead, evaluate on criteria using LangSmith.

### Test Layers

#### Layer 1 — Unit Tests (`tests/unit/`)

Test each tool and storage function in complete isolation. All external dependencies are mocked.

**Libraries:**
- `pytest` — test runner
- `pytest-asyncio` — async test support (the Telegram handlers are async; the tools and the FX fetch are sync)
- `moto[dynamodb]` — intercepts boto3 calls and emulates DynamoDB in-process; no Docker needed for unit tests
- `respx` — mocks `httpx` calls to `api.fxratesapi.com`

**What is tested:**

| Test file | Scenarios covered |
|---|---|
| `test_trip_time.py` | `local_date` gives the day in the given zone — behind UTC (Los Angeles, the evening of the previous day), ahead of it (Singapore, the morning of the next), and in UTC — and refuses a time with no offset; `describe_date` includes the weekday; `is_valid_timezone` accepts IANA names and rejects unknown, empty and path-like ones; `check_trip_status` uses the default zone with no trip, the trip's zone with one, and the default for a trip without a zone; the graph routes `tools_node` back through `check_trip_status`. Confirmed non-vacuous by mutation — ignoring the trip's zone and routing tools straight to the agent each fail a test |
| `test_trip.py` | `start_trip` records the local start date and zone, rejects an unknown zone, and refuses a second trip; `set_trip_timezone` moves the active trip and reports today in the new zone, and refuses with no trip or an unknown zone; `end_trip` returns the CSV and deletes all `EXPENSE#*` items and `TRIP#ACTIVE`; `end_trip` still exports and deletes when FX rates are unavailable, prefixing the no-SGD instruction; `end_trip` returns an error when no trip is active. Archive: every expense is copied in full with the start date and a `ttl` at the configured retention; the archive is invisible to the next trip's `EXPENSE#` queries; a failed archive write deletes nothing; an empty trip writes no archive. Confirmed non-vacuous by mutation — removing the archive fails three tests, moving it after the deletes fails one |
| `test_config.py` | `LOG_LEVEL` is upper-cased and whitespace-stripped; the normalised value is accepted by `logging`; unknown levels raise `ValidationError` |
| `test_expenses.py` | Ids use only the unambiguous alphabet; `add_expense` records under its id, defaults the payment method and date, retries a colliding id without overwriting, gives up after repeated collisions, and rejects invalid input; `edit_expense` updates fields in place, keeps the name unless a summary is given, edits a date without moving the key, compares `expected_amount` numerically, and refuses a mismatched amount, an unknown id, an invalid expected amount, nothing to change, or invalid values; `delete_expense` removes only its target and refuses a mismatch or unknown id; `get_all_expenses` lists ids for the model, sorted by date then creation time. Three regressions replay the September trip's failures: the batched date edits, the neighbouring-line pick, and the rename-on-every-edit. Confirmed non-vacuous by mutation — removing the amount check fails three tests, the date sort one, the conditional write four |
| `test_fx.py` | Successful rate fetch returns dict of rates; HTTP error raises a typed exception; unexpected response shape raises a typed exception |
| `test_dynamodb.py` | `put_item`, `get_item`, `delete_item`, `update_item` and `query_by_prefix` against moto; `query_by_prefix` returns every item across DynamoDB's 1 MB page boundary |
| `test_export.py` | `to_sgd` converts foreign currency, passes SGD through, and returns None for an unparseable amount or a missing rate; `generate_csv` emits the expected columns, populates `amount_sgd`, blanks it when no rate exists, and preserves the original amount and currency |
| `test_charts.py` | `generate_charts` returns PNG bytes for a populated trip, for an empty one, and when no expense has a usable rate |
| `test_chart_handler.py` | The chart Lambda returns both images base64-encoded, renders placeholders for a trip with no expenses, and rejects an event missing a required key or carrying a non-list `expenses` |
| `test_charts_client.py` | Decimal amounts serialise to strings and the payload survives `json.dumps`; the local path renders in-process and never invokes; the production path invokes with the expected payload and decodes both images; `None` is returned when the invoke is rejected, times out, reports a function error, returns an incomplete payload, or the function name is unset |
| `test_callback_guards.py` | `_acknowledge_callback` swallows an expired query as a repeated tap and re-raises anything else; `_claim_confirmation` rewrites the keyboard message with the progress notice, returns False when a second tap's identical edit is rejected as unmodified, and re-raises other failures |
| `test_chart_contract.py` | A payload built by the real `charts_client` serialiser, passed through an actual JSON round-trip into the real `chart_handler`. Each side's own tests mock the other, so the two could drift apart while both suites stayed green; this catches value-encoding drift in particular, since `chart_protocol` already prevents key renames. Confirmed non-vacuous by mutation — removing the `Decimal` conversion fails six tests |
| `test_auth.py` | `_check_auth`: first contact creates PENDING and notifies admin; PENDING/REJECTED/APPROVED return correct bool and send correct replies; group uses negative chat ID; missing username falls back to full name. `handle_auth_callback`: approve/reject update DynamoDB status and notify requester; missing auth record edits message without sending notification; group approval notifies the group chat |
| `test_graph.py` | `custom_routes` returns END for a non-AIMessage or a message with no tool calls, `end_trip` for a lone `end_trip` call, `end_trip_batch_error` for a mixed batch, and `tools` otherwise; `end_trip_batch_error_node` emits one rejecting `ToolMessage` per call in the batch |
| `test_telegram_handler.py` | `_extract_text`; `handle_admin_command` ignores non-admins, prints usage for no or unknown subcommand, lists records, approves, rejects, deletes, and reports a missing record |
| `test_reply_delivery.py` | Every reply goes out with no parse mode, including one containing a literal `<` — sent as HTML, that was rejected by Telegram in production. `_reply_in_chunks` and `_edit_in_chunks` deliver long content in order within the limit, the latter editing the first chunk in place and replying with the rest, and logging the dropped overflow when the message is inaccessible. Confirmed non-vacuous by mutation — reintroducing HTML for text containing `<` fails four tests |
| `test_end_trip_confirmation.py` | A resume that leaves the trip active is reported as a failure — no summary, no files, an error logged, the thread cleared — replaying the 9 Oct 2026 incident; a trip that did end sends the summary and files and clears the versioned thread; the graph is resumed on the versioned thread, once-per-turn durability; thread IDs carry the schema version. Confirmed non-vacuous by mutation — removing the trip check, dropping the version and dropping the durability each fail a test |
| `test_context_and_checkpointing.py` | `bounded_history` keeps a short conversation whole, cuts a long one to the budget at a user message, never separates a tool call from its result, sends an oversized current turn whole, and leaves a history with no user message untouched; `agent_node` sends the bounded history, not the full one. Against the real `DynamoDBSaver` on moto: saving once per turn writes at most 2 items, against over five times that per step; a turn saved once reloads with its whole conversation; a turn paused at the end-trip confirmation resumes and runs `end_trip`. Confirmed non-vacuous by mutation — sending the full history fails three tests, `"sync"` durability one |
| `test_deploy_lambda.py` | The profile comes from `--profile`, then `AWS_PROFILE`, then `aws_profile` in `local.auto.tfvars`, else the default chain; Terraform outputs are read, and a missing output or failed `terraform output` points at `apply` or `init`; `code_sha256` matches Lambda's encoding; a deploy uploads, updates from S3, waits, verifies and deletes; the staged zip is deleted even when the update fails; a checksum mismatch or failed update status is not reported as success; `main` refuses credentials for another account, deploys when the account matches, and stops on a missing archive |
| `test_update_dedup.py` | `put_item_if_absent` writes to a free key, leaves an existing item untouched and returns False, and raises other failures rather than reading them as a taken key; `claim_update` writes an expiring marker, refuses a second claim of the same `update_id`, and treats different ids independently; `lambda_handler` processes a first delivery, acknowledges a redelivery without processing it, drops a body with no `update_id`, and claims nothing for a forged delivery. Confirmed non-vacuous by mutation — skipping the claim fails one test, dropping the write's condition fails three |
| `test_prompts.py` | The system prompt differs with and without an active trip, and names the trip start date when one exists; it ends with "Today is <weekday, date> (<zone>)" and everything before that line is identical from day to day; it tells the model never to ask for the date, to infer the category, to default to Card, to keep expense ids from the user and never to re-add expenses from memory |

#### Layer 2 — Integration Tests (`tests/integration/`)

Test the full tool chain against a real DynamoDB Local instance (Docker). These tests verify that the actual boto3 queries, key structures, and DynamoDB response parsing all work together — things `moto` can occasionally diverge on.

**Requires:** `docker-compose up -d` before running. Skipped in CI unless the integration marker is explicitly requested.

**What is tested:**
- Full add → edit → delete → list flow for expenses
- `end_trip` produces correct category totals and clears all items
- Concurrent writes (two expenses added in quick succession) do not clobber each other

**Running:**
```bash
pytest tests/integration/ -m integration
```

#### Layer 2b — Manual end-to-end run via Telegram

Not automated, and worth keeping as a written procedure because it reaches paths nothing
else does: the live Bedrock loop, the inline keyboards, and whether the model's reported
figures actually match what the tools computed. Run it against DynamoDB Local with
`docker compose up -d`, then `uv run python -m src.bot.main`.

The sequence, and what each part is actually testing:

| Step | Verifies |
|---|---|
| Message the bot from an unapproved account, approve from the admin account | `_check_auth` first contact and `handle_auth_callback`; the Approve button's callback data is built from `AuthCommand`, so a mismatch breaks here |
| `/auth list`, `/auth`, `/auth banana` | The admin command's list, usage and unknown-subcommand branches |
| Start a trip; add expenses in three currencies, one with no currency named, one dated "yesterday" | `amount` persisted as a DynamoDB Number; the SGD default; relative date resolution against `message_date` |
| List, edit one amount, delete another, list again | Positional targeting; `update_item` rather than the transact path when the date is unchanged, which leaves the SK intact; `source_message` appended not replaced |
| End trip, confirm | The interrupt and inline keyboard; **compare the SGD total in the summary text against the sum of `amount_sgd` in the CSV** — a mismatch is the model inventing a rate rather than reading the tool output |
| Ask about the previous trip | `clear_thread_history`: the table should hold no checkpoint items and the agent should not recall the trip |
| Tap the confirmation button repeatedly | `_claim_confirmation` — one summary and one set of attachments, not several |

**What this cannot reach.** Locally `render_charts` takes the in-process branch, so the
boto3 invoke, `chart_handler` running as a Lambda, and the `lambda:InvokeFunction` grant
are all untouched by a green run here. The payload compatibility is covered by
`test_chart_contract.py`; the invoke itself is only exercised by a real trip end in
production, or by invoking the chart function directly after a deploy.

#### Layer 3 — LLM Evaluations (`tests/evals/`)

Evaluates the agent's LLM-driven behaviour using **LangSmith**. This is not run on every commit — it is run before a release or when the system prompt / model changes.

**What LangSmith provides:**
- **Tracing** — every live agent run is automatically logged (inputs, tool calls, LLM output, latency, token count). Free tier: 5,000 traces/month.
- **Datasets** — curated sets of `(input, expected_criteria)` pairs stored in LangSmith. You build these up over time as you find edge cases.
- **Evaluators** — functions that score a run. Can be rule-based (exact match on a field) or LLM-as-judge (Claude grades the output against a rubric).
- **Experiment tracking** — each eval run is versioned so you can compare scores before/after a prompt change.

**Datasets defined:**

`expense_extraction.json` — 20+ labelled examples testing the LLM's ability to parse a natural language expense message into structured fields.
```json
[
  {
    "input": "spent 1200 yen at Ichiran ramen for dinner yesterday",
    "expected": {
      "amount": "1200",
      "currency": "JPY",
      "category": "Food"
    }
  },
  {
    "input": "paid $50 for taxi",
    "expected": {
      "amount": "50",
      "currency": "SGD",
      "category": "Transport"
    }
  }
]
```

`intent_classification.json` — examples testing that the agent calls the correct tool.
```json
[
  { "input": "start a new trip", "expected_tool": "start_trip" },
  { "input": "remove the last expense", "expected_tool": "delete_expense" },
  { "input": "how much have I spent so far", "expected_tool": "get_all_expenses" },
  { "input": "end the trip", "expected_tool": "end_trip" }
]
```

**Evaluators defined in `evaluators.py`:**

| Evaluator | Type | Criteria |
|---|---|---|
| `field_extraction_accuracy` | Rule-based | Checks `amount`, `currency`, `category` match expected exactly |
| `summary_quality` | LLM-as-judge | Claude grades whether the generated `summary` reasonably describes the expense in the input |
| `tool_correctness` | Rule-based | Checks the first tool called matches `expected_tool` |
| `response_quality` | LLM-as-judge | Claude scores the bot's final reply on clarity and helpfulness (1–5) |
| `end_trip_confirmation` | LLM-as-judge | Claude grades whether the agent correctly called or refused `end_trip` based on the user's confirmation message; covers ambiguous replies ("yeah sure", "actually wait no") |
| `currency_extraction` | LLM-as-judge | Claude grades whether the agent correctly identified the currency from informal expressions ("quid", "bucks", "yuan") where exact-match rules are insufficient |

**Running evals:**
```bash
uv run python -m tests.evals.run_evals
```
Results appear in the LangSmith UI under the `expenses-bot` project.

### Coverage

**Not yet configured.** `pyproject.toml` has no `[tool.coverage]` sections, so there is no
`fail_under` gate and nothing enforces the targets below — they are goals, not guarantees.
Adding the config and ratcheting the threshold is an open Phase 3 item, and it only bites
once CI exists, since a local run can always be skipped.

The intended configuration:

```toml
[tool.coverage.run]
source = ["src"]
omit = ["src/bot/main.py"]   # Lambda/polling entrypoint — tested via integration

[tool.coverage.report]
fail_under = 80
show_missing = true
```

**Targets against measured coverage** (151 tests, `--cov=src`):

| Module | Target | Measured | |
|---|---|---|---|
| `tools/` | 90% | 100% | trip, expenses and fx all fully covered |
| `storage/` | 90% | 94% | |
| `agent/graph.py` | 70% | 85% | |
| `config.py` | 85% | 66% | the SSM loader only runs under `ENVIRONMENT=production` |
| `telegram_handler.py` | — | 58% | the largest gap; the async Telegram paths are the least covered |
| `main.py` | — | 0% | entrypoint, excluded by the `omit` above once configured |
| Overall | 80% | **78%** | |

**Running with coverage:**
```bash
uv run pytest --cov=src --cov-report=term-missing
```

### Dev Dependencies (`pyproject.toml`)

```toml
[dependency-groups]
# Deployed only in the chart Lambda, and selected on its own by
# `uv export --only-group charts`.
charts = [
    "matplotlib>=3.11.1",
]
dev = [
    "boto3-stubs[dynamodb]>=1.43.27",
    "moto[dynamodb]>=5.2.1",
    "mypy>=2.1.0",
    "pre-commit>=4.6.0",
    "pydantic[mypy]>=2.13.4",
    "pytest>=9.0.3",
    "pytest-asyncio>=1.4.0",
    "pytest-cov>=7.1.0",
    "respx>=0.23.1",
    "ruff>=0.15.17",
]

[tool.uv]
# `charts` is a deployment boundary, not an optional feature: local polling and the test
# suite both render in-process, so a plain `uv sync` must still install matplotlib. Only
# the main function's production artefact goes without it.
default-groups = ["dev", "charts"]
```

---

## Roadmap

Finished work is not listed here — git history has it, and the decisions that still
constrain the design are in [docs/decisions/](docs/decisions/). Only open items remain.

### Phase 1 — Local Development

Done: the bot, its tools, storage, LangGraph agent with checkpointing, access control with
admin approval and the `/auth` commands, and local polling.

- [ ] Integration tests against DynamoDB Local
- [ ] LangSmith project setup; build initial eval datasets; run first eval baseline

### Phase 2 — AWS Deployment

Done: the bot and chart Lambdas
([0001](docs/decisions/0001-chart-rendering-in-a-separate-lambda.md)), the HTTP API
webhook authenticated by the secret token
([0002](docs/decisions/0002-http-api-authenticated-by-secret-token.md)), secrets in SSM
([0003](docs/decisions/0003-secrets-and-account-id-out-of-the-repository.md)), Terraform
state in S3 ([0004](docs/decisions/0004-terraform-state-in-s3-bootstrap.md)),
point-in-time recovery on the table, and S3-staged deploys
([0009](docs/decisions/0009-deploy-through-temporary-artifacts-bucket.md)).

#### Security hardening
- [ ] CloudWatch structured logging validation

#### Bedrock Guardrails
- [ ] Denied topics policy: block off-topic requests (financial advice, general chat) and keep the agent scoped to expense tracking
- [ ] Prompt attack filter: detect injection attempts via user-supplied `source_message` (defence-in-depth against a compromised allowlisted account); guardrail ID + version added to `config.py` alongside model ID

---

### Phase 3 — CI/CD (GitHub Actions)

**Philosophy:** GitHub Actions is the CI/CD platform for this project. Concepts (pipelines, secrets management, environment promotion, deploy gates, OIDC credential federation) transfer directly to Jenkins or AWS CodePipeline — without the overhead of maintaining a CI server.

#### Workflows

```
.github/
└── workflows/
    ├── test.yml       # runs on every push/PR: unit tests + coverage gate
    └── deploy.yml     # runs on merge to main: package Lambda + deploy to prod
```

#### `test.yml` — Unit tests on every push

```
push / pull_request
        │
        ▼
  ubuntu-latest runner
        │
        ├── checkout code
        ├── install uv
        ├── uv sync --frozen
        └── pytest tests/unit/ --cov --cov-fail-under=80
```

- No AWS credentials needed — moto intercepts all boto3 calls in-process
- Fails the PR if coverage drops below 80%
- Runs on every push and every PR (including forks via `pull_request` trigger)

#### `deploy.yml` — Deploy to Lambda on merge to main

```
push to main (after test.yml passes)
        │
        ▼
  ubuntu-latest runner
        │
        ├── checkout code
        ├── install uv
        ├── uv run python scripts/build_lambda.py     # builds both archives
        └── uv run python scripts/deploy_lambda.py bot charts
                                                      # stages each in the artifacts bucket,
                                                      # updates, verifies, deletes
```

**AWS credential federation via OIDC (no long-lived keys):**
- GitHub Actions authenticates to AWS using OIDC — no `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` stored in GitHub secrets
- AWS IAM identity provider trusts `token.actions.githubusercontent.com`
- A deploy IAM role is assumed via `aws-actions/configure-aws-credentials`; scoped to `lambda:UpdateFunctionCode` + SSM read only
- The role trust policy restricts assumption to this specific repo and branch (`repo:owner/repo:ref:refs/heads/main`)

#### Secrets & environment variables

| Secret | Where stored | How accessed in Actions |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | AWS SSM Parameter Store | Lambda reads at startup via `config.py`; not in GitHub |
| AWS deploy role ARN | GitHub Actions secret (`AWS_DEPLOY_ROLE_ARN`) | Used by `configure-aws-credentials` step |
| LangSmith API key | GitHub Actions secret | Only present in eval workflow (future) |

No `.env` files in CI. No long-lived AWS keys anywhere.

#### Pre-commit hooks

Configured via `.pre-commit-config.yaml` (committed to repo). Run `pre-commit install` once after cloning to activate.

| Hook | What it catches |
|---|---|
| `ruff check --fix` | Lint errors, unused imports, undefined names |
| `ruff format` | Formatting inconsistencies |
| `mypy src/` | Type errors, missing annotations |

Run manually against all files:
```bash
uv run pre-commit run --all-files
```

#### Roadmap items

- [ ] `[tool.coverage.run]` / `[tool.coverage.report]` sections in `pyproject.toml`; ratchet `fail_under` up from the current level towards 80
- [ ] GitHub Actions `test.yml`: unit tests + coverage gate on every push/PR
- [ ] GitHub Actions `deploy.yml`: OIDC credential federation, Lambda packaging, deploy on merge to main
- [ ] IAM OIDC identity provider configured in AWS account
- [ ] Deploy IAM role with trust policy scoped to this repo + main branch
- [ ] Manual approval gate before prod deploy (GitHub Actions environment protection rule)
- [ ] Unit tests for `_split_message` in `test_telegram_handler.py`, which shipped without
  any. Cases: content exactly at 4096 characters stays one chunk; 4097 splits; the split
  falls on the last newline inside the window; a single line with no newline takes the
  hard cut at 4096; content needing three or more chunks; leading newlines are stripped
  from the next chunk and no chunk is empty; every chunk is at most 4096 characters and
  joining them loses no non-newline text

### Phase 4 — Ledger correctness (future)

Failures that change the wrong expense, or lose context, without any error. Done:
addressing expenses by id ([0006](docs/decisions/0006-expenses-addressed-by-id-with-amount-check.md)),
trip time zones ([0007](docs/decisions/0007-trip-time-zone-and-local-dates.md)), and
dropping redelivered updates ([0008](docs/decisions/0008-redelivered-updates-dropped-by-update-id.md)).

- [ ] **Turns that exceed the gateway's 30 s limit** still show the generic error before
  their real reply, though the repeat is now dropped. First remedy: bound the context
  (Phase 6). If slow turns persist after that, move to an SQS FIFO queue per
  [0008](docs/decisions/0008-redelivered-updates-dropped-by-update-id.md) — which would
  also fix the concurrent-turn item below
- [ ] **Concurrent turns share one checkpoint thread.** Two members messaging at the same
  time produce two Lambda invocations against the same `thread_id`, each reading and
  writing the whole conversation state. Expense data is unaffected — the tools write to
  DynamoDB directly, under distinct sort keys — but one invocation can overwrite history
  that never included the other's turn, so the agent loses context it appeared to have.
  Observed working in the first group test; the failure is silent when it does occur
- [ ] **Expenses record no author.** `source_message` keeps what was typed but not who
  typed it, so a shared ledger cannot answer "who paid for the broccoli" — which is the
  first question anyone splitting costs will ask. Needs a field on the expense item, and
  surfacing in `get_all_expenses` and the CSV
- [ ] **The partition key still reads `USER#<ledger_id>`** while holding a group ID. A
  rename to something neutral is a data migration, so it is deliberately deferred; the
  cheapest moment is whenever the table is next empty

### Phase 5 — Enhancements (future)
- [ ] Receipt image parsing (user sends photo, agent extracts expense via vision)
- [ ] Budget alerts (warn user when spending exceeds a threshold)
- [ ] FX rate caching per day (avoid redundant API calls for same currency on same day)
- [ ] Reply to messages the bot cannot read. Photos, videos, voice notes and documents
  match no handler (`filters.TEXT` only), so they get no reply at all — including a photo
  whose caption says "lunch 12", since a caption is not `message.text`. A handler on those
  types replying "I can only read text for now — tell me the amount and I'll log it" turns
  the silence into an answer. Superseded for photos once receipt parsing lands

### Phase 6 — Model, context and cost (future)

Measured on the September trip (11 Sep – 7 Oct 2026, Haiku 4.5) from Bedrock's CloudWatch
metrics: 297 model calls, **10.23M input tokens**, 55K output tokens, no cache reads or
writes. The largest single request grew from 3.5K tokens on day one to 90K by the end,
because each call resends the whole trip's checkpointed conversation. Input is therefore
over 99% of the bill, roughly $10.50 for the trip at Anthropic's list price — Bedrock's
own Claude prices were not verified, as its pricing page renders those tables client-side.

Done: history sent to the model is bounded to ~8K tokens, and the conversation is saved
once per turn rather than after every step
([0013](docs/decisions/0013-bounded-history-and-once-per-turn-checkpoints.md)). Verify
on the next trip with the same CloudWatch metrics: `InputTokenCount` per call should
plateau rather than grow with the trip.
- [ ] **Move the chat model to Claude Haiku 5.5.** `global.anthropic.claude-haiku-5-5` is
  ACTIVE in ap-southeast-1 (checked 8 Oct 2026). List price $0.10 / $0.50 per MTok for
  prompts up to 100K tokens and $0.50 / $2.50 above, against $1 / $5 for Haiku 4.5; the
  same text is ~30% more tokens, so roughly 7–8× cheaper per call below 100K; the history
  bound keeps calls well below it. Required changes, each of which otherwise fails or
  degrades the first request:
  - Add prompt caching with it. The fixed prefix — system prompt and tool definitions —
    is ~3.3K tokens (approximate), below Haiku 4.5's 4,096-token minimum, so caching does
    nothing today; Haiku 5.5's minimum is 512. Place a `cachePoint` by hand after the
    stable part of the system prompt, before the "Today is" line. Do not use
    `ChatBedrockConverse`'s `cache_control` option: it also marks the latest message,
    which under a sliding history window pays the 25% cache-write premium on every call
    for a prefix never reused. Verify that `CacheReadInputTokenCount` becomes non-zero
  - Remove `temperature=0.3` from `ChatBedrockConverse` in `nodes.py`: any non-default
    sampling parameter returns a 400. Consistency comes from the prompt, the validating
    tools and `effort`
  - Thinking is adaptive and on by default. Set `effort` (`low` or `medium` for chat)
    through the Converse request fields, and leave `max_tokens` room for thinking.
    Verify, before switching production, that `langchain-aws` returns reasoning blocks
    to the model unchanged across checkpointed turns — editing earlier turns invalidates
    them. The graph is append-only apart from `clear_thread_history` at trip end, which
    removes the thread whole. `_extract_text` already reads text blocks by type
  - Handle `stop_reason == "refusal"`: Bedrock has no server-side fallback, so a decline
    must produce a clear reply rather than the generic error
  - Update `bedrock_model_id` in `terraform.tfvars`. The IAM grant is derived from it
    (`main.tf`, the inference-profile and foundation-model ARNs), so it follows
  - Confirm Bedrock's per-token price before switching, and re-baseline cost dashboards
    for the new tokenizer rather than reading the jump as a regression
  - Run the manual end-to-end script (Layer 2b) against the new model first; the Layer 3
    evals are still unbuilt
- [ ] **End-of-trip analysis by a larger model, from totals the code computes.** The trip
  summary is meant as analysis — patterns, where the money went, outliers — which is the
  one step where a stronger model earns its cost. On the September trip it also got the
  arithmetic wrong: it reported SGD 3,629.34 for a CSV totalling SGD 3,413.08 (Food
  1,033.61 vs 977.07, Leisure 148.37 vs 94.37, Misc 32.64 vs 52.74), having summed 88
  rows itself. A larger model makes that rarer, not impossible, so both changes go
  together:
  - Compute the figures in `end_trip` from the same data as the CSV: overall SGD total,
    per category, per day, per currency where rates are missing, and the largest
    expenses. Pass them in the tool result with an instruction to quote them as given
  - Write the summary in a dedicated graph node on Claude Sonnet 5.5 or Opus 5.5 (both
    ACTIVE as `global.` profiles in ap-southeast-1), leaving the chat on Haiku. It runs
    once per trip on ~5–10K input tokens: roughly $0.02–0.05 on Sonnet 5.5 and $0.05–0.10
    on Opus 5.5 at list price
  - Same request changes as Haiku 5.5 (no `temperature`, adaptive thinking, refusal
    handling), plus a second model ID in Terraform and its own IAM grant, since the
    current grant is derived from the single chat model

### Phase 7 — Environments (future)

- [ ] **UAT environment on AWS.** Everything today is tested in production or against
  DynamoDB Local. A UAT stack should mirror production — same Terraform, same services —
  so that what passes there predicts production. Two shapes, decision pending:
  - *Same account, second stack*: an `environment` variable prefixing every resource
    name, its own table, Lambdas and SSM paths, its own state key in the state bucket, and
    a second Telegram bot from BotFather. Cheapest; shares the account's IAM and quotas
  - *Separate AWS account* (recommended): the same stack in its own account under AWS
    Organizations. Full isolation; `allowed_account_ids` already supports one config per
    account
- [ ] **Azure — decision pending.** The goal is learning a second cloud, not testing this
  bot: an Azure UAT would run different storage, secrets, model and entry-point code, so
  its results would not transfer to production. Eleven of the twenty-one source files
  call an AWS service directly (DynamoDB, the DynamoDB checkpointer, Bedrock through
  `langchain-aws`, SSM, the API Gateway event shape, the chart Lambda invoke, and
  `botocore` exceptions). Options under consideration:
  - Port the bot as a second deployment target: put each AWS dependency behind an
    interface with an AWS and an Azure implementation (Functions, Key Vault, a database
    with a LangGraph checkpointer, Claude on Microsoft Foundry), production staying on
    AWS. Unverified: LangChain's support for Foundry, and a LangGraph checkpointer for
    Cosmos DB (one exists for PostgreSQL)
  - Learn Azure on separate small projects instead — a daily FX-rate function serving
    this bot (Phase 5), a receipt reader on Document Intelligence, a past-trips dashboard
    on Static Web Apps
