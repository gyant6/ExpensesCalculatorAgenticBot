# 0001. Render charts in a separate Lambda function

- **Status:** Accepted
- **Date:** 2026-08-17

## Context

The first single-function build measured 67.8 MB zipped — past Lambda's 50 MB direct-upload
limit. matplotlib, Pillow, fontTools and kiwisolver accounted for much of it, and existed
only for the two PNGs sent at trip end. Worse, `charts.py` imported matplotlib at module
scope and `tools/trip.py` imported from it, so every cold start — including one that only
records an expense — loaded the plotting stack.

## Decision

Charts render in their own function, `ExpensesCalculatorAgenticBot-charts`:

- `export.py` holds the CSV and SGD conversion; `charts.py` holds plotting only.
- matplotlib is a PEP 735 dependency group (`charts`), so `uv export --only-group charts`
  builds the chart artefact and the bot's artefact excludes it.
- The bot invokes the chart function synchronously through `charts_client.py`, with
  explicit timeouts. Locally, `charts_client` imports `charts` inside the function and
  renders in-process — a module-level import would fail at cold start in production.
- `chart_protocol.py` holds the payload keys with no imports, so both sides share the
  contract without the chart function acquiring the bot's configuration.
- The chart function's role has CloudWatch Logs only. The bot's role gains
  `lambda:InvokeFunction` on that one function.

## Consequences

- Measured after the split: `function.zip` 44.9 MB (88% of the cap), `chart_function.zip`
  39.2 MB. numpy stays in the bot's artefact because `langchain-aws` depends on it, but
  the bot never imports the plotting stack. Cold starts measured 4366 ms (bot) and
  2431 ms (charts).
- A chart failure returns `None` and the summary and CSV still go out; charts are a
  convenience.
- The timeouts are an ordering: `chart_lambda_timeout < chart_client_timeout <
  lambda_timeout`, so a slow render is abandoned before it can kill the turn.
- A synchronous invoke caps payloads at 6 MB; base64 PNGs fit far below that. If they
  ever did not, the chart function would write to a bucket and return the key.
- If the bot's artefact outgrows the cap, the lever is botocore (14 MB), which the
  runtime already provides — at the cost of pinning to AWS's version.

## Alternatives considered

- **Upload through S3 to lift the 50 MB limit.** Fixes size, not the cold start on every
  message.
- **The chart function reads expenses from DynamoDB itself.** Rejected: it would need a
  role with read access to every ledger, and the bot already holds the data. As a pure
  function of its input it holds no state and no data access.
