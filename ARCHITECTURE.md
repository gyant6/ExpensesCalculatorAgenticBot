# Architecture: Overseas Expenses Telegram Bot

## Overview

An agentic Telegram chatbot that helps users track overseas travel expenses. The agent uses LangGraph for stateful conversation management, Claude Haiku on AWS Bedrock as the LLM, and DynamoDB as the sole database (conversation history + expenses). Designed for local development first, with a clear migration path to serverless AWS (API Gateway + Lambda).

---

## Tech Stack

| Concern | Choice | Reason |
|---|---|---|
| LLM | Claude Haiku (`anthropic.claude-haiku-4-5-20251001`) on Bedrock | Fast responses, low cost, sufficient for structured extraction |
| Agent framework | LangGraph | Production-standard stateful agent; checkpointing built-in; CV-worthy |
| LLM integration | `langchain-aws` (`ChatBedrock`) | First-class LangChain/LangGraph integration with Bedrock |
| Telegram | `python-telegram-bot` | Well-maintained, supports both polling (local) and webhook (Lambda) |
| Database | DynamoDB (single-table) | Native CRUD, serverless, free tier sufficient |
| Local DB | DynamoDB Local (Docker) | Identical boto3 API; switch via `DYNAMODB_ENDPOINT_URL` env var |
| Conversation state | LangGraph DynamoDB checkpointer (`langgraph-checkpoint-dynamodb`) | Persists full graph state per user; delete checkpoint = clear history |
| Packaging | `uv` + `pyproject.toml` | Modern Python standard; fast installs; clean Lambda packaging |
| Charts | `matplotlib` | Generate pie chart PNG in memory; send as Telegram photo |
| FX rates | `api.fxratesapi.com` | Free, no auth required, simple GET |

---

## Repository Layout

```
ExpensesCalculatorAgenticBot/
├── pyproject.toml
├── .env.example
├── .env                          # gitignored
├── docker-compose.yml            # DynamoDB Local
├── src/
│   ├── __init__.py
│   └── bot/
│       ├── __init__.py
│       ├── main.py               # entrypoint (polling locally, Lambda handler in prod)
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── graph.py          # LangGraph graph definition
│       │   ├── state.py          # AgentState TypedDict
│       │   └── prompts.py        # system prompt
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── trip.py           # start_trip, end_trip
│       │   ├── expenses.py       # add, edit, delete, list expenses
│       │   └── fx.py             # get_exchange_rate
│       ├── storage/
│       │   ├── __init__.py
│       │   └── dynamodb.py       # DynamoDB client + table operations
│       ├── telegram_handler.py   # receives Telegram updates, calls agent
│       └── config.py             # settings via pydantic-settings
└── tests/
    ├── __init__.py
    ├── unit/
    │   ├── __init__.py
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
        │   ├── expense_extraction.json   # 20+ labelled examples
        │   └── intent_classification.json
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
        ├──► AWS Bedrock (us-east-1 or ap-southeast-2)
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
        │         ├── Conversation checkpoints (langgraph-checkpoint-dynamodb)
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

### Layer B — User allowlist (telegram_handler.py, local and prod)

`telegram_handler.py` checks the `telegram_user_id` from the incoming update against `ALLOWED_TELEGRAM_IDS` (a comma-separated list in the environment / SSM) before invoking the agent. Unauthorized users receive no response and no Lambda compute is wasted on the agent graph.

This check must be the first operation in the handler, before any DynamoDB or Bedrock calls.

---

## LangGraph Agent Design

### Graph Structure

```
START
  │
  ▼
[check_trip_status node]
  │  (reads TRIP#ACTIVE from DynamoDB, sets trip_start_date in state)
  ▼
[agent node]  ◄─────────────────────┐
  │                                  │
  ├── if tool_calls in response ─►  [tools node]
  │                                  │  interrupt_before=["end_trip"]:
  │                                  │  graph pauses, saves state to checkpointer,
  │                                  │  returns control to telegram_handler.py which
  │                                  │  sends confirmation prompt to user; resumes
  │                                  │  on next message
  │                                  │
  │                                ──┘ (tool results appended to messages)
  │
  │  (if no tool_calls — final response)
  ▼
END
```

This is a standard ReAct loop implemented as a LangGraph graph. The `agent` node calls Claude Haiku with the bound tools. If the model decides to call a tool, the `tools` node executes it and the result is appended to state. The loop continues until Claude returns a plain message.

`end_trip` is configured with `interrupt_before=["end_trip"]` at graph compilation time. This provides one structural guarantee: `end_trip` can never execute on the same turn the LLM first decides to call it. When the LLM emits a tool call for `end_trip`, the graph pauses before the tools node runs, persists state to the checkpointer, and returns. The handler detects the interrupted state via `graph.get_state(config).next` (non-empty when interrupted) and forwards the LLM's last message to the user as the confirmation prompt. On the next user message, the graph resumes from the checkpoint — the agent node runs again with the full conversation history and decides whether to proceed with `end_trip` based on the user's response. The LLM's instruction-based behaviour (see `end_trip` docstring) handles the confirmation logic; `interrupt_before` is the structural backstop that prevents single-turn deletion regardless of LLM behaviour.

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
    chart_bytes: bytes | None   # Set by end_trip via Command return after generating the pie
                                # chart; read by telegram_handler.py after graph finishes to
                                # send the chart as a Telegram photo; None on all other turns
```

### Checkpointing (Conversation Memory)

- `thread_id` = `str(telegram_user_id)`
- LangGraph checkpointer (`langgraph-checkpoint-dynamodb`) persists the full `messages` list to DynamoDB after every graph step
- When a trip ends: `end_trip` tool deletes all expense/trip items AND deletes the checkpoint for this `thread_id`
- When a trip starts: fresh checkpoint begins automatically on next message

---

## DynamoDB Table Design (Single-Table)

**Table name:** `expenses-bot` (configurable via env)

**Primary key:** `PK` (String) + `SK` (String)

| PK | SK | Attributes | Description |
|---|---|---|---|
| `USER#<telegram_user_id>` | `TRIP#ACTIVE` | `start_date` | Active trip marker |
| `USER#<telegram_user_id>` | `EXPENSE#<datetime>` | see below | Individual expense |

**Expense item attributes:**

| Attribute | Type | Example |
|---|---|---|
| `PK` | String | `USER#123456789` |
| `SK` | String | `EXPENSE#2026-06-04T14:32:05.123456+00:00` |
| `date` | String (ISO-8601) | `2026-06-04` |
| `raw` | String | `1200 yen at Ichiran ramen for dinner` |
| `category` | String | `Food & Dining` |
| `currency` | String | `JPY` |
| `amount` | Decimal | `1200` |
| `merchant` | String | `Ichiran` |
| `summary` | String | `Dinner at Ichiran ramen` |
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
- **Input:** `date: str`, `raw: str`, `amount: float`, `currency: str`, `merchant: str`, `category: str`, `summary: str`
- **Action:** Writes `EXPENSE#<datetime>` item (SK = `datetime.utcnow().isoformat()`) with the raw amount and currency as provided. No FX conversion at write time.
- **Returns:** Expense summary.
- **Note:** The LLM extracts all structured fields from the user's raw message. If the user does not mention a currency, the LLM defaults `currency` to `"SGD"`.

### 3. `edit_expense`
- **Input:** `expense_id: str`, and any subset of editable fields (`amount`, `currency`, `merchant`, `category`, `summary`, `date`)
- **Action:** Updates the specified fields. Updates `updated_at`.
- **Returns:** Updated expense summary.

### 4. `delete_expense`
- **Input:** `expense_id: str`
- **Action:** Deletes the `EXPENSE#<expense_id>` item.
- **Returns:** Confirmation.

### 5. `get_all_expenses`
- **Input:** _(none beyond user_id)_
- **Action:** Queries all `EXPENSE#*` items for this user. Also fetches `TRIP#ACTIVE` for trip metadata.
- **Returns:** Structured list of all expenses + trip metadata.
- **Note:** If no `TRIP#ACTIVE` item exists, returns a signal that no trip is active. The agent then tells the user to start a trip first.

### 6. `end_trip`
- **Input:** _(none beyond user_id)_
- **Human-in-the-loop:** The graph is compiled with `interrupt_before=["end_trip"]`. This guarantees `end_trip` never executes on the same turn the LLM first decides to call it — the graph always pauses and returns to the handler first. The `end_trip` docstring instructs the LLM to ask for confirmation and call `get_all_expenses` before invoking this tool. On resume (next user message), the agent node runs again with full conversation history and calls `end_trip` if the user confirmed.
- **Action (in order, after interrupt resumes):**
  1. Calls `get_sgd_exchange_rates()` to get current FX rates.
  2. Converts each expense to SGD using the fetched rates (`sgd_amount = amount / rates[currency]`; no conversion needed when `currency == "SGD"`).
  3. Generates summary text (totals by category, grand total in SGD, per-expense table).
  4. Generates matplotlib pie chart PNG in memory.
  5. Deletes all `EXPENSE#*` items and the `TRIP#ACTIVE` item.
  6. Deletes the LangGraph DynamoDB checkpoint for this `thread_id`.
  7. Returns the summary text + chart image to the Telegram handler.
- **Returns:** A confirmation string to the LLM. Chart bytes are written to `AgentState.chart_bytes` via a `Command` return — `telegram_handler.py` reads this after the graph finishes and sends the chart as a separate Telegram photo message.

### 7. `get_sgd_exchange_rates`
- **Input:** _(none)_
- **Action:** `GET https://api.fxratesapi.com/latest?base=SGD`. Fetches all rates with SGD as the base.
- **Returns:** `dict` mapping currency codes to their rate relative to SGD (e.g. `{"JPY": 167.5, "USD": 0.74}`). To convert a foreign amount to SGD: `sgd_amount = foreign_amount / rates[currency]`.
- **Note:** Called internally by `end_trip` only. Not directly exposed to the LLM.

---

## Expense Parsing Flow (AI-Assisted Structured Extraction)

The LLM extracts structured fields from the user's natural language before calling `add_expense`. This is handled inside the agent loop — the model is prompted to identify these fields before invoking the tool:

```
Example A — foreign currency explicitly mentioned:
  User: "spent 1200 yen at Ichiran ramen for dinner yesterday"
  LLM extracts: date=2026-06-03, amount=1200, currency=JPY, merchant=Ichiran,
                category=Food & Dining, summary="Dinner at Ichiran ramen"
  Stored as-is: amount=1200, currency=JPY (no FX call at write time)

Example B — no currency mentioned, defaults to SGD:
  User: "paid $12 for chicken rice at Maxwell"
  LLM extracts: date=2026-06-04, amount=12, currency=SGD, merchant=Maxwell Food Centre,
                category=Food & Dining, summary="Chicken rice at Maxwell"
  Stored as-is: amount=12, currency=SGD

FX conversion happens once at end_trip:
  get_sgd_exchange_rates() → {"JPY": 167.5, ...}
  JPY expense: sgd_amount = 1200 / 167.5 = 7.16
  SGD expense: sgd_amount = 12 (no conversion)
```

---

## Trip Summary (end_trip output)

Sent to the user as two Telegram messages:

**Message 1 — Text (Markdown):**
```
*Trip Summary*
Started: 4 Jun 2026 | All amounts in SGD

*Expenses by Category*
| Category       | Total (SGD) |
|----------------|-------------|
| Food & Dining  | $45.20      |
| Transport      | $28.50      |
| Accommodation  | $210.00     |
| Shopping       | $95.30      |

*Grand Total: SGD 379.00*

*AI Analysis*
Your largest spending category was Accommodation (55%).
You spent an average of SGD 54.14/day...
[etc]

*All Expenses*
| Date  | Merchant   | Category      | Amount       | SGD   |
|-------|------------|---------------|--------------|-------|
| 04/06 | Ichiran    | Food & Dining | 1,200 JPY    | 11.50 |
| ...   | ...        | ...           | ...          | ...   |
```

**Message 2 — Photo:**
Matplotlib pie chart of spending by category (PNG, generated in memory).

---

## Environment Configuration

```bash
# .env.example

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here

# AWS
AWS_REGION=ap-southeast-2
AWS_ACCESS_KEY_ID=          # local: from ~/.aws/credentials; Lambda: IAM role
AWS_SECRET_ACCESS_KEY=      # local: from ~/.aws/credentials; Lambda: IAM role

# DynamoDB
DYNAMODB_TABLE_NAME=expenses-bot
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
- `pytest-asyncio` — async test support (all tool functions are async)
- `moto[dynamodb]` — intercepts boto3 calls and emulates DynamoDB in-process; no Docker needed for unit tests
- `respx` — mocks `httpx` calls to `api.fxratesapi.com`

**What is tested:**

| Test file | Scenarios covered |
|---|---|
| `test_trip.py` | `start_trip` creates item; second `start_trip` returns error; `end_trip` deletes all user items and checkpoint |
| `test_expenses.py` | `add_expense` writes item with raw amount and currency; `edit_expense` updates only the specified fields; `delete_expense` removes correct item; `get_all_expenses` returns empty signal when no `TRIP#ACTIVE` exists |
| `test_fx.py` | Successful rate fetch returns dict of rates; HTTP error raises a typed exception; unexpected response shape raises a typed exception |
| `test_dynamodb.py` | `put_item`, `get_item`, `delete_item`, `query_by_prefix` work against moto |

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
      "amount": 1200,
      "currency": "JPY",
      "merchant": "Ichiran",
      "category": "Food & Dining"
    }
  },
  {
    "input": "paid $50 for taxi",
    "expected": {
      "amount": 50,
      "currency": "SGD",
      "merchant": null,
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
| `merchant_extraction` | LLM-as-judge | Claude grades whether the extracted merchant is reasonable given the input |
| `tool_correctness` | Rule-based | Checks the first tool called matches `expected_tool` |
| `response_quality` | LLM-as-judge | Claude scores the bot's final reply on clarity and helpfulness (1–5) |

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
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "moto[dynamodb]>=5.0",
    "respx>=0.21",
    "langsmith>=0.1",
]
```

---

## Roadmap

### Phase 1 — Local Development
- [ ] Project scaffolding: `uv init`, `pyproject.toml`, `.env`, `docker-compose.yml` for DynamoDB Local
- [ ] `config.py` with pydantic-settings
- [ ] DynamoDB table creation script (run once locally and in prod)
- [ ] Storage layer: `dynamodb.py` — low-level DynamoDB client wrapper
- [ ] Tool implementations (trip, expenses, fx rate)
- [ ] Unit tests for all tools and storage layer (moto + respx); coverage ≥ 80%
- [ ] Integration tests against DynamoDB Local
- [ ] LangGraph graph: state, agent node, tools node, DynamoDB checkpointer
- [ ] System prompt engineering
- [ ] Telegram polling handler (local mode)
- [ ] `end_trip` summary: text generation + matplotlib chart
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

- [ ] `.pre-commit-config.yaml`: ruff (lint + format) + mypy
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
- [ ] User allowlist (`ALLOWED_TELEGRAM_IDS`) check in `telegram_handler.py`
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
