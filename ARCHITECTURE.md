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
        │  (telegram_user_id as thread_id)
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

**Approval request sent to admin (Telegram ID `35153600`):**

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
[check_trip_status]
  │  (reads TRIP#ACTIVE from DynamoDB, sets trip_start_date in state)
  ▼
[agent_node] ◄────────────────────────────────────────────────┐
  │                                                           │
  │  custom_routes(state) inspects the last message:          │
  │                                                           │
  ├── not an AIMessage, or no tool_calls ─────────────► END    │
  │                                                           │
  ├── end_trip is the only tool call                          │
  │        └──► [end_trip_node] ───────────────────────────────┤
  │               interrupt_before: graph pauses here          │
  │               before the tool executes, persists           │
  │               state, and returns to the handler            │
  │                                                           │
  ├── end_trip batched with other tools                       │
  │        └──► [end_trip_batch_error_node] ───────────────────┤
  │               injects a rejecting ToolMessage for every    │
  │               call in the batch, so the model retries      │
  │               with end_trip alone                          │
  │                                                           │
  └── only non-end_trip tools                                 │
           └──► [tools_node] ──────────────────────────────────┘
                  start_trip, add_expense, edit_expense,
                  delete_expense, get_all_expenses
```

The batch case exists because routing a mixed batch either way loses a call silently:
`tools_node` has no `end_trip` bound, and `end_trip_node` has none of the others. Worse,
`tools_node` is not interrupted, so a batched `end_trip` would skip the confirmation
entirely. Rejecting the whole batch is the only option that neither drops a tool call nor
deletes a trip without asking.

This is a standard ReAct loop implemented as a LangGraph graph. `agent_node` calls Claude Haiku with the bound tools. `custom_routes` then inspects the last message: anything that is not an `AIMessage` carrying tool calls ends the turn, an `end_trip` call on its own routes to `end_trip_node`, `end_trip` mixed with other tools routes to `end_trip_batch_error_node`, and any other tool call routes to `tools_node`. All three tool nodes edge back to `agent_node`, so the loop continues until Claude returns a plain message.

`end_trip` sits in its own node so that `interrupt_before=["end_trip_node"]` pauses that one tool without interrupting any of the others. This provides one structural guarantee: `end_trip` can never execute on the same turn the LLM first decides to call it. When the LLM emits an `end_trip` tool call, the graph pauses before the node runs, persists state to the checkpointer, and returns. `handle_message` detects the interrupted state via `graph.get_state(config).next` (non-empty when interrupted) and sends a Yes/No inline keyboard.

Confirmation arrives as a callback query rather than as a new text message. `handle_callback` first renders the chart and CSV attachments from the still-live expense data, then resumes with `graph.invoke(None, config)` so `end_trip_node` genuinely executes: the tool performs the export and the deletion and returns the CSV that `agent_node` turns into a summary. Once that summary and the attachments have been delivered, the handler calls `clear_thread_history` to delete the thread's checkpoints. While the graph is interrupted, `handle_message` declines new text messages and asks the user to confirm or cancel first.

### State Definition

```python
# src/bot/agent/state.py
from langgraph.graph import MessagesState

class AgentState(MessagesState):
    # MessagesState provides: messages: list[BaseMessage]
    # thread_id is managed by the checkpointer config, not state
    telegram_user_id: str       # Set by telegram_handler.py on every invocation; injected into
                                # tools via InjectedState so the LLM never sees or supplies it
    message_date: str           # YYYY-MM-DD date of the incoming Telegram message; set by
                                # telegram_handler.py; used by add_expense as fallback date when
                                # the user does not explicitly mention one
    trip_start_date: str | None # Set by check_trip_status node; passed to system prompt so the
                                # LLM knows whether a trip is active and when it started
```

### Checkpointing (Conversation Memory)

- `thread_id` = `str(telegram_user_id)`
- LangGraph checkpointer (`langgraph-checkpoint-aws`, `DynamoDBSaver`) persists the full `messages` list to DynamoDB after every graph step
- When a trip ends: the `end_trip` tool deletes all expense and trip items, then the caller (`telegram_handler` or `dev_runner`) calls `clear_thread_history` to delete every checkpoint for this `thread_id`. The deletion cannot live in the tool — `agent_node` writes the summary after the tool returns, using the very history being deleted, so it runs only once the summary and attachments have been delivered
- When a trip starts: a fresh checkpoint begins automatically on the next message

---

## DynamoDB Table Design (Single-Table)

**Table name:** `ExpensesCalculator` (configurable via `DYNAMODB_TABLE_NAME`)

**Primary key:** `PK` (String) + `SK` (String)

| PK | SK | Attributes | Description |
|---|---|---|---|
| `USER#<telegram_user_id>` | `TRIP#ACTIVE` | `start_date` | Active trip marker |
| `USER#<telegram_user_id>` | `EXPENSE#<datetime>` | see below | Individual expense |
| `AUTH#<id>` | `PROFILE` | `status`, `entity_type`, `username`, `requested_at`, `reviewed_at` | Access control record (user or group) |

`<id>` is the Telegram user ID (positive) or group ID (negative). `entity_type` is `USER` or `GROUP`. `status` is `PENDING`, `APPROVED`, or `REJECTED`.

**Expense item attributes:**

| Attribute | Type | Example |
|---|---|---|
| `PK` | String | `USER#123456789` |
| `SK` | String | `EXPENSE#2026-06-04T14:32:05.123456+00:00` |
| `date` | String (ISO-8601) | `2026-06-04` |
| `source_message` | String | `1200 yen at Ichiran ramen for dinner` |
| `category` | String | `Food` |
| `currency` | String | `JPY` |
| `amount` | Number (Decimal) | `1200` |
| `summary` | String | `Dinner at Ichiran ramen` |
| `payment_method` | String | `Cash` |
| `updated_at` | String (ISO-8601) | `2026-06-04T13:45:00.000000+00:00` |

**Local vs prod switch:** Set `DYNAMODB_ENDPOINT_URL=http://localhost:8000` in local `.env`. Unset (or absent) in prod — boto3 connects to real DynamoDB automatically.

---

## Tools

All tools are LangChain `@tool`-decorated functions. `telegram_user_id` is injected from `AgentState` by the LangGraph tool node — the LLM never sees it as a parameter. `telegram_handler.py` is responsible for setting both `telegram_user_id` and `message_date` in state before invoking the graph.

### 1. `start_trip`
- **Input:** _(none)_
- **Action:** Checks if `TRIP#ACTIVE` exists. If yes, returns error (only 1 active trip). Otherwise writes `TRIP#ACTIVE` item with `start_date`.
- **Returns:** Confirmation with start date.

### 2. `add_expense`
- **Input:** `source_message: str`, `summary: str`, `category: str`, `amount: str`, `currency: str`, `date: str | None = None`, `payment_method: str = "Cash"`
- **Action:** Writes an `EXPENSE#<datetime>` item (SK = `EXPENSE#` + `datetime.now(timezone.utc).isoformat()`) with the amount and currency as provided. No FX conversion at write time. When `date` is None, falls back to `message_date` from state.
- **Returns:** `"Expense recorded."`, or a validation error string describing what was invalid.
- **Note:** The LLM extracts all structured fields from the user's raw message. If the user does not mention a currency, the LLM defaults `currency` to `"SGD"`. `category` must be one of the values in `CATEGORIES`; `amount` must parse as a positive `Decimal`; `date` must be `YYYY-MM-DD`.

### 3. `edit_expense`
- **Input:** `expense_num: int` (1-based index as shown by `get_all_expenses`), `edit_message: str`, `summary: str`, and any subset of `category`, `amount`, `currency`, `date`, `payment_method`
- **Action:** Updates the supplied fields and `updated_at`, appending `edit_message` to the existing `source_message`. When `date` changes the SK must change too, so the item is moved via a single `transact_write_items` (delete + put) instead of updated in place.
- **Returns:** `"Edit expense successful."`, or an error string if no optional field was supplied or a value failed validation.

### 4. `delete_expense`
- **Input:** `expense_num: int` (1-based index as shown by `get_all_expenses`)
- **Action:** Queries all `EXPENSE#*` items for this user and deletes the one at that position.
- **Returns:** `"Expense deleted."`, or an error string if the index is out of range.

### 5. `get_all_expenses`
- **Input:** _(none beyond user_id)_
- **Action:** Queries all `EXPENSE#*` items for this user.
- **Returns:** A numbered, pipe-delimited list — `summary | category | amount currency | date | payment_method` — or a message indicating no expenses are recorded. The 1-based position in this list is what `edit_expense` and `delete_expense` take as `expense_num`.

### 6. `end_trip`
- **Input:** _(none beyond user_id)_
- **Human-in-the-loop:** The graph is compiled with `interrupt_before=["end_trip_node"]`. This guarantees `end_trip` never executes on the same turn the LLM first decides to call it. The graph pauses, saves state to the checkpointer, and returns control to `handle_message`, which sends a Yes/No inline keyboard to the user.
- **Action (once the node runs, after confirmation):**
  1. Returns an error and deletes nothing if no `TRIP#ACTIVE` item exists.
  2. Queries all `EXPENSE#*` items for the user.
  3. Calls `get_sgd_exchange_rates()`. On failure it continues without rates rather than blocking the trip from ending.
  4. Builds the CSV via `generate_csv(expenses, fx_rates)` — before any deletion, so a failed export leaves the trip intact rather than destroying records with no copy of them.
  5. Deletes every `EXPENSE#*` item and the `TRIP#ACTIVE` item.
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
| `test_trip.py` | `start_trip` creates item; second `start_trip` returns error; `end_trip` returns the CSV and deletes all `EXPENSE#*` items and `TRIP#ACTIVE`; `end_trip` still exports and deletes when FX rates are unavailable, prefixing the no-SGD instruction; `end_trip` returns an error when no trip is active |
| `test_config.py` | `LOG_LEVEL` is upper-cased and whitespace-stripped; the normalised value is accepted by `logging`; unknown levels raise `ValidationError` |
| `test_expenses.py` | `add_expense` writes item with raw amount and currency; `edit_expense` updates only the specified fields; `delete_expense` removes correct item; `get_all_expenses` returns a no-expenses message when the user has none |
| `test_fx.py` | Successful rate fetch returns dict of rates; HTTP error raises a typed exception; unexpected response shape raises a typed exception |
| `test_dynamodb.py` | `put_item`, `get_item`, `delete_item`, `update_item`, `transact_write_delete_put` and `query_by_prefix` against moto; `query_by_prefix` returns every item across DynamoDB's 1 MB page boundary |
| `test_export.py` | `to_sgd` converts foreign currency, passes SGD through, and returns None for an unparseable amount or a missing rate; `generate_csv` emits the expected columns, populates `amount_sgd`, blanks it when no rate exists, and preserves the original amount and currency |
| `test_charts.py` | `generate_charts` returns PNG bytes for a populated trip, for an empty one, and when no expense has a usable rate |
| `test_chart_handler.py` | The chart Lambda returns both images base64-encoded, renders placeholders for a trip with no expenses, and rejects an event missing a required key or carrying a non-list `expenses` |
| `test_charts_client.py` | Decimal amounts serialise to strings and the payload survives `json.dumps`; the local path renders in-process and never invokes; the production path invokes with the expected payload and decodes both images; `None` is returned when the invoke is rejected, times out, reports a function error, returns an incomplete payload, or the function name is unset |
| `test_chart_contract.py` | A payload built by the real `charts_client` serialiser, passed through an actual JSON round-trip into the real `chart_handler`. Each side's own tests mock the other, so the two could drift apart while both suites stayed green; this catches value-encoding drift in particular, since `chart_protocol` already prevents key renames. Confirmed non-vacuous by mutation — removing the `Decimal` conversion fails six tests |
| `test_auth.py` | `_check_auth`: first contact creates PENDING and notifies admin; PENDING/REJECTED/APPROVED return correct bool and send correct replies; group uses negative chat ID; missing username falls back to full name. `handle_auth_callback`: approve/reject update DynamoDB status and notify requester; missing auth record edits message without sending notification; group approval notifies the group chat |
| `test_graph.py` | `custom_routes` returns END for a non-AIMessage or a message with no tool calls, `end_trip` for a lone `end_trip` call, `end_trip_batch_error` for a mixed batch, and `tools` otherwise; `end_trip_batch_error_node` emits one rejecting `ToolMessage` per call in the batch |
| `test_telegram_handler.py` | `_parse_mode` and `_extract_text`; `handle_admin_command` ignores non-admins, prints usage for no or unknown subcommand, lists records, approves, rejects, deletes, and reports a missing record |
| `test_prompts.py` | The system prompt differs with and without an active trip, and names the trip start date when one exists |

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
`test_chart_contract.py`; the invoke itself is what the Step 4 smoke test is for.

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

### Phase 1 — Local Development
- [x] Project scaffolding: `uv init`, `pyproject.toml`, `.env`, `docker-compose.yml` for DynamoDB Local
- [x] `config.py` with pydantic-settings
- [x] Storage layer: `dynamodb.py` — low-level DynamoDB client wrapper
- [x] Tool implementations (trip, expenses, fx rate)
- [ ] Unit tests for all tools and storage layer (moto + respx); coverage ≥ 80%
- [ ] Integration tests against DynamoDB Local
- [x] LangGraph graph: state, agent node, tools node, DynamoDB checkpointer
- [x] System prompt engineering
- [x] Telegram polling handler (local mode)
- [x] `end_trip` summary: text generation + matplotlib chart
- [x] Clear conversation history when a trip ends: `graph.checkpointer.delete_thread(thread_id)`, called from `handle_callback` and `dev_runner` after the summary and attachments have been delivered. It cannot live inside `end_trip` — `agent_node` writes the summary after the tool returns and needs the message history to do it. Wrap it so a failed deletion cannot fail the user's turn after they already have their summary
- [x] `enable_checkpoint_compression=True` on `DynamoDBSaver`, which gzips each snapshot before writing and expands it on read (measured ~4.6x). Reduces DynamoDB read and write units only — the state reaching Bedrock is decompressed and identical, so token cost is unchanged
- [x] `ttl_seconds` on `DynamoDBSaver`, plus TTL enabled on the table itself, so abandoned threads expire with no active code path required
- [ ] Prune checkpoint versions within a long trip: `checkpointer.prune([thread_id], strategy="keep_latest")` retains only the most recent checkpoint per namespace. Bounds storage but not token cost — the retained checkpoint still holds the full `messages` list
- [x] Validate and upper-case `LOG_LEVEL` at settings load, so an invalid value fails with a message naming the setting and the accepted levels rather than a bare `ValueError` raised inside the logging module at import
- [x] Paginate `query_by_prefix` via the boto3 paginator: a DynamoDB `query` returns at most 1 MB per call, and ignoring `LastEvaluatedKey` silently returned a partial list beyond that. Covered by a unit test that crosses the real 1 MB boundary — moto enforces the same cap, and a single query returned only 83 of 120 padded items
- [x] Store `amount` as a DynamoDB Number rather than String, using `Decimal` because boto3 refuses Python floats. Makes it numerically comparable and stops every consumer re-parsing it
- [x] Wire up or remove `AWS_BEDROCK_PROFILE` — removed, since the Bedrock client never read it
- [x] Harden `custom_routes` to match any `end_trip` tool call rather than only `tool_calls[0]`. A batch mixing `end_trip` with other tools now routes to `end_trip_batch_error_node`, which injects a rejecting `ToolMessage` for every call so the model retries with `end_trip` alone — neither silently skipping `end_trip` nor bypassing the confirmation interrupt
- [x] Access control: `AUTH#<id>` DynamoDB items, PENDING/APPROVED/REJECTED states
- [x] Admin approval flow: unknown users trigger Approve/Reject message to admin via inline keyboard
- [x] Group ID support: approve `AUTH#<group_id>` (negative) independently of user-level access
- [x] `ADMIN_TELEGRAM_ID` in config (env var / SSM in prod)
- [x] Manual end-to-end testing via Telegram
- [x] Admin command interface: `/auth` is registered as a dedicated `CommandHandler`, which PTB routes directly without passing through `handle_message` or the auth gate. The handler silently ignores the command if `effective_user.id != ADMIN_TELEGRAM_ID` — this is the sole guard, since the auth gate never runs for command handlers. Supported commands:
  - `/auth list` — list all `AUTH#*` records with their status, entity type, and username
  - `/auth approve <id>` — set status to APPROVED
  - `/auth reject <id>` — set status to REJECTED
  - `/auth delete <id>` — delete the record entirely so the entity can re-apply from scratch
- [ ] LangSmith project setup; build initial eval datasets; run first eval baseline

### Phase 2 — AWS Deployment

Tackled in order so the bot is running in prod as early as possible, with security hardening layered on after.

#### Step 1 — Lambda handler (code only, no AWS resources yet)
- [x] Adapt `main.py` to support webhook mode: `lambda_handler(event, context)` parses the Telegram JSON from the API Gateway event body and dispatches via PTB. Polling mode (`if __name__ == "__main__"`) continues to work unchanged for local dev. Both modes share `_build_app()` so handler registration is never duplicated.
- [x] Validate handler locally with a synthetic API Gateway event payload before provisioning anything.

#### Step 2 — Core infrastructure (Terraform)
- [x] Terraform: Lambda function + IAM execution role (DynamoDB read/write + Bedrock invoke, SSM GetParameters — least-privilege)
- [x] Terraform: prod DynamoDB table (same key schema as local, TTL enabled on `ttl` attribute)
- [x] Sensitive secrets (`TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_ID`) are stored in SSM Parameter Store as SecureString. Terraform creates the parameters with a `REPLACE_ME` placeholder and `ignore_changes = [value]`, so the real value set via CLI is never overwritten by a subsequent apply and never stored in Terraform state. Lambda env vars hold the SSM paths; `config.py` fetches and injects them into `os.environ` before pydantic-settings loads, only when `ENVIRONMENT=production`.
- [x] Lambda packaging: `scripts/build_lambda.py` — `uv export --no-dev` → `uv pip install` → zip dependencies + src/. Dependencies are resolved for Python 3.13 on `x86_64-unknown-linux-gnu` rather than for the build machine, so a Windows or macOS build produces the same Linux artefact as the CI runner

#### Step 3 — Split chart rendering into its own Lambda

The first build measured **67.8 MB zipped, 197.5 MB unzipped** — past Lambda's 50 MB direct-upload limit. matplotlib, Pillow, fontTools and kiwisolver account for much of that and exist solely for the two PNGs sent at trip end. Splitting them into a second function puts both artefacts under the limit, so neither needs S3 staging.

Measured after the split:

| Archive | Zipped | % of the 50 MB cap | Largest components |
|---|---:|---:|---|
| `function.zip` | 44.9 MB | 88% | botocore 14.0, numpy 15.7, zstandard 5.3 |
| `chart_function.zip` | 39.2 MB | 78% | matplotlib 9.3, numpy 15.7, Pillow 6.5, fontTools 4.6 |

Two things this measurement corrected. **numpy stays in the main function regardless** — `langchain-aws` depends on it directly, so it is not part of what leaves with matplotlib. And the main archive sits at 88% of the cap, not the roughly 30 MB estimated before building, leaving about 5 MB of headroom. If that runs out, the lever is botocore: at 14.0 MB it is the single largest item, and the Lambda runtime already provides boto3 and botocore, so excluding them would bring the archive to about 31 MB at the cost of pinning to whatever version AWS ships.

The cold-start benefit is unaffected by numpy remaining, because bytes in the artefact are not the same as modules imported. Verified directly: importing `src.bot.main` pulls in none of matplotlib, numpy, Pillow, fontTools or kiwisolver.

Size is what forces the decision, but cold start is the better reason for it. `charts.py` imports matplotlib at module scope and `tools/trip.py` imports `generate_csv` from that same module, so matplotlib and numpy load on **every** cold start of the main function, including one that only records an expense. After the split the main function has no import path to matplotlib at all, and only trip end pays that cost. For a personal bot that idles long enough for containers to be reaped, most messages are cold starts, so this trades a cost on the frequent path for one on the rare path.

- [x] Extract the matplotlib-free code out of `charts.py` into `src/bot/export.py`: `CSV_FIELDNAMES`, `generate_csv` and `to_sgd` (made public, since it is now shared across modules). `charts.py` keeps only the plotting functions. Imports updated in `tools/trip.py` and `telegram_handler.py`
- [x] Move matplotlib into its own PEP 735 dependency group: `[dependency-groups] charts = [...]`, with `[tool.uv] default-groups = ["dev", "charts"]` so it stays installed locally for polling and the tests. A group rather than an optional-dependency extra because `uv export` offers `--only-group` but has no `--only-extra`, and the chart artefact must contain matplotlib and nothing else
- [x] `scripts/build_lambda.py`: emit two archives — `function.zip` from `uv export --no-dev --no-default-groups`, `chart_function.zip` from `uv export --only-group charts` — and report both against the 50 MB limit. Both are built even if the first is oversized, so one run reports every size
- [x] New handler `src/bot/chart_handler.py`: `lambda_handler(event, context)` taking `{"expenses": [...], "fx_rates": {...}}` and returning base64-encoded PNGs. Reads `LOG_LEVEL` straight from the environment rather than through `config.py`, which would require the bot token and admin ID this function has no business holding
- [x] New client `src/bot/charts_client.py`: `render_charts(expenses, fx_rates)` invokes the chart function synchronously via boto3 with explicit connect and read timeouts, and returns `None` on any failure. A chart failure must not cost the user their summary or CSV — the same graceful degradation already applied when FX rates are unavailable. Decimal amounts are serialised to strings, not floats, because `to_sgd` parses them back through `Decimal` and a float round-trip would reintroduce the representation error the Number migration removed
- [x] `src/bot/chart_protocol.py`: the four payload keys, with no imports of its own, so both sides agree on the contract without the chart function acquiring the bot's configuration or dependencies
- [x] `config.py`: `CHART_LAMBDA_FUNCTION_NAME` and `CHART_LAMBDA_TIMEOUT_SECONDS`, plus a `PRODUCTION_ENVIRONMENT` constant replacing the `"production"` literal. Local polling renders in-process rather than invoking, via an import inside the function — `charts.py` ships in both artefacts but matplotlib does not, so a module-level import would work locally and fail at cold start in production
- [x] Terraform: second `aws_lambda_function` for charts with its own execution role carrying CloudWatch Logs only — it touches no DynamoDB, Bedrock or SSM, and reusing the bot's role would hand a renderer full read/write access to every expense. The bot's role gains `lambda:InvokeFunction` scoped to the chart function ARN, its only cross-function permission
- [x] Terraform: `lifecycle { ignore_changes = [filename, source_code_hash] }` on both functions, so code deployed by the AWS CLI is not rolled back by a later `terraform apply`. Terraform owns the infrastructure; the CLI owns the code. Same reasoning as the `ignore_changes` already on the SSM placeholder values
- [x] Removed `AWS_REGION` from the bot's Lambda environment block. It is a reserved Lambda environment variable, set by the runtime, and supplying it in the function configuration is rejected at deploy time — this would have failed the first `terraform apply`. `config.py` still reads it, because the runtime provides it
- [x] `terraform.tfvars.example`, since no variable carries a default and `apply` would otherwise prompt for all seven. The three chart timeouts are documented as an ordering — `chart_lambda_timeout < chart_client_timeout < lambda_timeout` — so a slow render is abandoned by the chart function first and the bot's client second, leaving the bot alive to deliver the summary without charts
- [x] Verified with `terraform fmt -check` and `terraform validate` — both clean against Terraform 1.15.8 and AWS provider 5.100.0
- [x] Corrected the Bedrock IAM resource. `AWS_BEDROCK_MODEL_ID` names a global *inference profile*, not a foundation model, and the policy granted `foundation-model/global.anthropic.claude-haiku-...` — an ARN matching nothing, so every model call would have been denied. Invoking through a profile is authorised against both the profile ARN and the foundation models it routes to, so the policy now grants both; the foundation model ID is derived by stripping the routing prefix
- [x] Read the account ID from `data.aws_caller_identity` instead of an `aws_account_id` variable. A hand-entered ID that disagrees with the credentials in use is not an error: the IAM policies are built naming another account, grant nothing, and fail only at runtime as AccessDenied
- [x] Commit `terraform.tfvars` rather than gitignoring it. Secrets never belonged there — they are SSM SecureStrings — and with the account ID now derived, nothing in the file is sensitive. Keeping deployment configuration out of version control leaves the repo unable to reproduce its own infrastructure
- [x] Commit `.terraform.lock.hcl`, which HashiCorp intends to be version-controlled. The constraint is `~> 5.0`, so without the lock a later machine resolves a different 5.x and nothing records which provider version built the running infrastructure
- [x] Unit tests: the chart handler returns PNGs for a representative payload and rejects a malformed event; `render_charts` returns `None` and logs when the invoke is rejected, times out, reports a function error, or returns a payload missing an image
- [x] Rebuild and confirm both archives are under 50 MB zipped and each unzipped size is under 250 MB
- [x] `boto3-stubs[dynamodb]` → `boto3-stubs[dynamodb,lambda]` in the dev group. Without the Lambda stubs the `LambdaClient` annotation degraded to `Any` and mypy accepted both a misspelled method and a misspelled keyword argument on the `invoke` call. It matters more than usual here because that call never executes locally and the tests mock it away, so the type checker is the only thing inspecting it before production. Adding them immediately caught `InvocationType` widening to `str` instead of the literal the API accepts

Constraints worth recording:
- A synchronous invoke caps request and response at 6 MB each. This is a limit on the data passed between the two functions and is unrelated to the 50 MB deployment limit above. PNGs must be base64-encoded to travel in a JSON response, which inflates them by about a third, so the usable image budget is nearer 4.5 MB. Two charts and a small expense list sit far below that. If they ever approached it, the chart function would write the PNG to a bucket and return the object key instead of the bytes
- Lambda allocates CPU in proportion to memory, so under-provisioning the chart function shows up as slow renders rather than as errors
- The chart function is a pure function of its input — expenses and FX rates in, PNG bytes out. It reads no database and holds no state, which is what makes it separable at all

#### Step 4 — Build and deploy
- [ ] Build both archives: `uv run python scripts/build_lambda.py`
- [ ] Deploy: `terraform apply`
- [ ] Set real SSM values: `aws ssm put-parameter --name /ExpensesCalculatorAgenticBot/telegram-bot-token --value "<token>" --type SecureString --overwrite --region ap-southeast-1` (and same for admin-telegram-id)
- [ ] Push code for both functions: `aws lambda update-function-code --function-name <name> --zip-file fileb://<archive>`
- [ ] Smoke-test: invoke the main Lambda directly with a synthetic payload via `aws lambda invoke`
- [ ] Record `Init Duration` from the CloudWatch log for a cold start, so the cold-start cost of the split is measured rather than assumed

#### Step 5 — API Gateway + webhook
- [ ] Terraform: HTTP API Gateway (POST /webhook → Lambda integration)
- [ ] Register webhook URL with Telegram (`setWebhook`)
- [ ] End-to-end test via Telegram

#### Step 6 — Security hardening
- [ ] Webhook secret token: set `secret_token` at `setWebhook` registration; validate `X-Telegram-Bot-Api-Secret-Token` header in handler before processing
- [ ] API Gateway resource policy: IP allowlist from Telegram's published CIDR ranges ([cidr.txt](https://core.telegram.org/resources/cidr.txt))
- [ ] CIDR updater Lambda + EventBridge weekly schedule (keeps IP allowlist in sync with Telegram's published ranges)
- [ ] IAM role for CIDR updater scoped to `apigateway:UpdateRestApiPolicy` on the webhook API ARN only
- [ ] CloudWatch structured logging validation

#### Step 7 — Bedrock Guardrails
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
        ├── aws lambda update-function-code --function-name <main>  --zip-file fileb://function.zip
        └── aws lambda update-function-code --function-name <chart> --zip-file fileb://chart_function.zip
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

- [x] `.pre-commit-config.yaml`: ruff (lint + format) + mypy
- [ ] `[tool.coverage.run]` / `[tool.coverage.report]` sections in `pyproject.toml`; ratchet `fail_under` up from the current level towards 80
- [ ] GitHub Actions `test.yml`: unit tests + coverage gate on every push/PR
- [ ] GitHub Actions `deploy.yml`: OIDC credential federation, Lambda packaging, deploy on merge to main
- [ ] IAM OIDC identity provider configured in AWS account
- [ ] Deploy IAM role with trust policy scoped to this repo + main branch
- [ ] Manual approval gate before prod deploy (GitHub Actions environment protection rule)

### Phase 4 — Enhancements (future)
- [ ] Receipt image parsing (user sends photo, agent extracts expense via vision)
- [ ] Budget alerts (warn user when spending exceeds a threshold)
- [ ] FX rate caching per day (avoid redundant API calls for same currency on same day)
