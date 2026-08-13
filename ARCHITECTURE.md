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
| Charts | `matplotlib` | Generate pie chart (by category) and bar chart (by day) as PNGs in memory; sent as Telegram photos on trip end |
| CSV export | Python stdlib `csv` | Generate expense CSV with SGD-equivalent column on trip end; sent as Telegram file attachment |
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
│       ├── charts.py             # pie chart, bar chart, CSV generation for end_trip
│       ├── telegram_handler.py   # receives Telegram updates, calls agent
│       └── config.py             # settings via pydantic-settings
└── tests/
    ├── __init__.py
    ├── conftest.py               # moto fixtures + table creation
    ├── unit/
    │   ├── __init__.py
    │   ├── test_config.py
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
  Lambda Function
        │
        ├──► DynamoDB (real, same table schema)
        │         ├── Conversation checkpoints (langgraph-checkpoint-aws)
        │         └── Trip + Expense items
        ├──► Bedrock (Claude Haiku, same region)
        └──► api.fxratesapi.com
```

The only code difference between local and prod is configuration — no business logic changes.

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

1. Look up `AUTH#<user_id>` (and `AUTH#<chat_id>` if the message is from a group).
2. `APPROVED` → proceed normally.
3. `PENDING` → reply "Your access request is already pending approval." Do nothing else.
4. `REJECTED` → silently ignore.
5. Not found → create `AUTH#<user_id>` with `status=PENDING`, then send an approval request to the admin.

**Approval request sent to admin (Telegram ID `35153600`):**

```python
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("Approve", callback_data=json.dumps({"action": "auth:approve", "id": requester_id, "type": "USER"}))],
    [InlineKeyboardButton("Reject",  callback_data=json.dumps({"action": "auth:reject",  "id": requester_id, "type": "USER"}))],
])
message = f"Access request from @{username} (ID: {requester_id})"
```

Callback prefix `auth:` is handled by a dedicated `CallbackQueryHandler` in `main.py`, parallel to the existing `end_trip:` handler. On Approve/Reject, the handler updates `AUTH#<id>.status` in DynamoDB and notifies the requester.

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
[agent_node] ◄──────────────────────────────────────────┐
  │                                                     │
  │  custom_routes(state) inspects the last message:     │
  │                                                     │
  ├── no tool_calls ────────────────────────────► END    │
  │                                                     │
  ├── tool_calls[0]["name"] == "end_trip"                │
  │        └──► [end_trip_node] ─────────────────────────┤
  │               interrupt_before: graph pauses here    │
  │               before the tool executes, persists     │
  │               state, and returns to the handler      │
  │                                                     │
  └── any other tool                                    │
           └──► [tools_node] ────────────────────────────┘
                  start_trip, add_expense, edit_expense,
                  delete_expense, get_all_expenses
```

This is a standard ReAct loop implemented as a LangGraph graph. `agent_node` calls Claude Haiku with the bound tools. `custom_routes` then inspects the last message: no tool calls ends the turn, an `end_trip` call routes to `end_trip_node`, and any other tool call routes to `tools_node`. Both tool nodes edge back to `agent_node`, so the loop continues until Claude returns a plain message.

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
| `amount` | String | `1200` |
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
- **Note:** Synchronous, because its caller `end_trip` is a sync tool executing inside `graph.invoke` and cannot await. Called twice per trip end — once inside `end_trip` for the CSV, once in `_render_attachments` for the charts. Not bound to the LLM.

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

**Message 2 — Photo:** Pie chart of spending by category (PNG, generated in memory via `matplotlib`).

**Message 3 — Photo:** Bar chart of daily spending in SGD (PNG, generated in memory via `matplotlib`).

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
AWS_BEDROCK_PROFILE=        # optional; named AWS profile for local Bedrock calls
AWS_ACCESS_KEY_ID=          # local: from ~/.aws/credentials; Lambda: IAM role
AWS_SECRET_ACCESS_KEY=      # local: from ~/.aws/credentials; Lambda: IAM role

# DynamoDB
DYNAMODB_TABLE_NAME=ExpensesCalculator
DYNAMODB_ENDPOINT_URL=http://localhost:8000   # remove this line in prod

# App
LOG_LEVEL=INFO
ENVIRONMENT=local   # or: production

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

Configured in `pyproject.toml`. Coverage is measured over `src/` only.

```toml
[tool.coverage.run]
source = ["src"]
omit = ["src/bot/main.py"]   # Lambda/polling entrypoint — tested via integration

[tool.coverage.report]
fail_under = 80
show_missing = true
```

**Targets by module:**

| Module | Target | Rationale |
|---|---|---|
| `tools/` | 90% | Pure business logic; fully unit-testable |
| `storage/` | 90% | Deterministic DynamoDB wrappers |
| `agent/graph.py` | 70% | Graph wiring; LLM calls excluded |
| `config.py` | 85% | Straightforward but worth checking env var handling |
| Overall | 80% | CI hard minimum — PR fails below this |

**Running with coverage:**
```bash
pytest tests/unit/ --cov --cov-report=term-missing --cov-report=html
```

### Dev Dependencies (`pyproject.toml`)

```toml
[dependency-groups]
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
```

---

## Roadmap

### Phase 1 — Local Development
- [x] Project scaffolding: `uv init`, `pyproject.toml`, `.env`, `docker-compose.yml` for DynamoDB Local
- [x] `config.py` with pydantic-settings
- [ ] DynamoDB table creation script (run once locally and in prod)
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
- [ ] `ttl_seconds` on `DynamoDBSaver`, plus TTL enabled on the table itself, so abandoned threads expire with no active code path required
- [ ] Prune checkpoint versions within a long trip: `checkpointer.prune([thread_id], strategy="keep_latest")` retains only the most recent checkpoint per namespace. Bounds storage but not token cost — the retained checkpoint still holds the full `messages` list
- [x] Validate and upper-case `LOG_LEVEL` at settings load, so an invalid value fails with a message naming the setting and the accepted levels rather than a bare `ValueError` raised inside the logging module at import
- [x] Paginate `query_by_prefix` via the boto3 paginator: a DynamoDB `query` returns at most 1 MB per call, and ignoring `LastEvaluatedKey` silently returned a partial list beyond that. Covered by a unit test that crosses the real 1 MB boundary — moto enforces the same cap, and a single query returned only 83 of 120 padded items
- [ ] Store `amount` as a DynamoDB Number rather than String, using `Decimal` because boto3 refuses Python floats. Makes it numerically comparable and stops every consumer re-parsing it
- [ ] Wire up or remove `AWS_BEDROCK_PROFILE` — declared in `config.py` and referenced nowhere, so setting it currently has no effect
- [ ] Harden `custom_routes` to match any `end_trip` tool call rather than only `tool_calls[0]`. If the model ever emits `get_all_expenses` and `end_trip` in one message, the batch routes to `tools_node`, where `end_trip` is not bound, and the confirmation interrupt never fires
- [ ] Access control: `AUTH#<id>` DynamoDB items, PENDING/APPROVED/REJECTED states
- [ ] Admin approval flow: unknown users trigger Approve/Reject message to admin via inline keyboard
- [ ] Group ID support: approve `AUTH#<group_id>` (negative) independently of user-level access
- [ ] `ADMIN_TELEGRAM_ID` in config (env var / SSM in prod)
- [ ] Manual end-to-end testing via Telegram
- [ ] LangSmith project setup; build initial eval datasets; run first eval baseline

### Phase 2 — CI/CD (GitHub Actions)

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
        ├── uv export --no-dev -o requirements.txt
        ├── pip install -r requirements.txt --target package/
        ├── zip -r function.zip package/ src/
        └── aws lambda update-function-code --zip-file fileb://function.zip
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

---

### Phase 3 — AWS Deployment
- [ ] Lambda handler (`main.py` webhook mode)
- [ ] API Gateway setup (POST /webhook)
- [ ] API Gateway resource policy: IP allowlist from Telegram's CIDR ranges
- [ ] API Gateway webhook secret token (`X-Telegram-Bot-Api-Secret-Token`) validation in handler
- [ ] Register Telegram webhook URL with BotFather (set `secret_token` at registration time)
- [ ] IAM role with least-privilege DynamoDB + Bedrock permissions
- [ ] Lambda packaging via `uv export --no-dev` + zip or container image
- [ ] Environment variables in Lambda (no `.env` file — use SSM Parameter Store or Lambda env vars)
- [ ] CIDR updater Lambda + EventBridge weekly schedule (keeps API Gateway IP allowlist in sync with Telegram's published ranges)
- [ ] IAM role for CIDR updater Lambda scoped to `apigateway:UpdateRestApiPolicy` on the webhook API ARN only
- [ ] CloudWatch structured logging validation
- [ ] Bedrock Guardrails — denied topics policy to block off-topic requests (financial advice, general chat, etc.) and keep the agent scoped to expense tracking
- [ ] Bedrock Guardrails — prompt attack filter to detect injection attempts via user-supplied `source_message` (defence-in-depth against a compromised allowlisted account); guardrail ID + version added to `config.py` alongside model ID

### Phase 4 — Enhancements (future)
- [ ] Receipt image parsing (user sends photo, agent extracts expense via vision)
- [ ] Budget alerts (warn user when spending exceeds a threshold)
- [ ] FX rate caching per day (avoid redundant API calls for same currency on same day)
