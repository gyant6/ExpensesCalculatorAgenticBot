"""Evaluate complete conversations using the production graph and conversation operations.

By default this prints the evaluation plan without connecting to AWS or a database.
Paid execution requires --allow-paid, explicit token prices and request/cost limits.
Each run creates its own DynamoDB Local table and removes only that table afterwards.
Message times, expense timestamps and FX rates come from the scenario fixtures.
Reports contain outcomes, transcripts, per-request latency, usage and estimated cost.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ToolMessage,
    messages_to_dict,
)
from langchain_core.messages.utils import count_tokens_approximately
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from src.bot.agent.nodes import ChatModel

DEFAULT_SCENARIOS = Path(__file__).with_name("evaluation_scenarios.json")
EVALUATION_TABLE_PREFIX = "ExpensesModelEval_"
MILLION = Decimal(1_000_000)


class ExpenseExpectation(BaseModel):
    """The complete user-visible fields of one expected stored expense."""

    model_config = ConfigDict(extra="forbid")
    summary: str
    amount: Decimal = Field(gt=0)
    currency: str
    category: str
    date: str
    payment_method: str = "Card"


class Expectation(BaseModel):
    """Expected database state and optional reply/tool checks after a turn."""

    model_config = ConfigDict(extra="forbid")
    trip_active: bool
    timezone: str | None = None
    expenses: list[ExpenseExpectation] = Field(default_factory=list)
    awaiting_confirmation: bool = False
    archived_expenses: int | None = None
    reply_contains: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    preserve_expenses: list[str] = Field(default_factory=list)
    tool_results_contain: list[str] = Field(default_factory=list)


class AmountConflict(BaseModel):
    """Simulate a concurrent amount change before the first valid edit/delete check."""

    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1)
    amount: Decimal = Field(gt=0, allow_inf_nan=False)


class Step(BaseModel):
    """One message, explicit confirmation or cancellation at a fixed instant."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["message", "confirm", "cancel"] = "message"
    text: str | None = None
    message_time: AwareDatetime
    expected: Expectation
    amount_conflict: AmountConflict | None = None

    @model_validator(mode="after")
    def check_text(self) -> Step:
        """Require message text only on message actions."""
        if self.action == "message" and not (self.text and self.text.strip()):
            raise ValueError("A message action requires non-empty text")
        if self.action != "message" and self.text is not None:
            raise ValueError("Confirmation actions do not accept message text")
        return self


class Scenario(BaseModel):
    """A fresh isolated ledger and the turns to evaluate on it."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    steps: list[Step] = Field(min_length=1)


class Suite(BaseModel):
    """Public, fictional conversations and their fixed exchange rates."""

    model_config = ConfigDict(extra="forbid")
    fx_rates: dict[str, float]
    scenarios: list[Scenario] = Field(min_length=1)

    @model_validator(mode="after")
    def check_suite(self) -> Suite:
        """Reject duplicate names and unusable fixed exchange rates."""
        if len({item.name for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("Scenario names must be unique")
        if not self.fx_rates or any(
            not 0 < rate < float("inf") for rate in self.fx_rates.values()
        ):
            raise ValueError("FX rates must be finite and positive")
        return self


class BudgetExceeded(RuntimeError):
    """A request would exceed the request count or estimated-cost allowance."""


@dataclass
class MeteredModel:
    """Enforce limits before every model call and retain its usage/latency record.

    Cost is an estimate using caller-supplied per-million-token prices. Before each
    request it reserves estimated input plus the configured maximum output. Actual
    reported usage replaces that reservation afterwards; this is not a billing cap.
    """

    model: ChatModel
    max_requests: int
    max_cost: Decimal
    input_price: Decimal
    output_price: Decimal
    max_output_tokens: int
    tool_tokens: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    cost: Decimal = Decimal(0)

    def invoke(self, messages: list[BaseMessage]) -> BaseMessage:
        """Invoke within budget; propagate errors after recording the attempted call."""
        estimated_input = (
            int(count_tokens_approximately(messages) * 2) + self.tool_tokens
        )
        reserved = (
            estimated_input * self.input_price
            + self.max_output_tokens * self.output_price
        ) / MILLION
        if len(self.calls) >= self.max_requests or self.cost + reserved > self.max_cost:
            raise BudgetExceeded(
                "Stopped before inference: request or estimated-cost limit reached"
            )
        record: dict[str, Any] = {
            "request": len(self.calls) + 1,
            "estimated_input_tokens": estimated_input,
        }
        self.calls.append(record)
        self.cost += reserved
        started = time.perf_counter()
        try:
            response = self.model.invoke(messages)
            if not isinstance(response, AIMessage):
                raise TypeError("The model must return an AIMessage")
            usage = response.usage_metadata
            if usage is not None:
                actual = (
                    usage["input_tokens"] * self.input_price
                    + usage["output_tokens"] * self.output_price
                ) / MILLION
                self.cost += actual - reserved
                record.update(
                    usage=dict(usage),
                    estimated_cost_usd=str(actual),
                    usage_estimated=False,
                )
            else:
                record.update(estimated_cost_usd=str(reserved), usage_estimated=True)
            record["response"] = messages_to_dict([response])[0]
            return response
        except Exception as error:
            record.update(
                error=f"{type(error).__name__}: {error}",
                estimated_cost_usd=str(reserved),
                usage_estimated=True,
            )
            raise
        finally:
            record["seconds"] = round(time.perf_counter() - started, 6)


def validate_local_endpoint(endpoint: str | None) -> str:
    """Require an explicit loopback HTTP endpoint for evaluation storage."""
    parsed = urlparse(endpoint or "")
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "Evaluation storage must use an explicit loopback HTTP endpoint"
        )
    return str(endpoint)


def _expense_signature(item: dict[str, Any]) -> tuple[str, ...]:
    """Compare complete expense sets without depending on generated storage IDs."""
    fields = ("summary", "currency", "category", "date", "payment_method")
    return (
        *(str(item[name]).casefold() for name in fields),
        str(Decimal(str(item["amount"])).normalize()),
    )


@contextmanager
def _amount_conflict(
    ledger_id: str, conflict: AmountConflict | None
) -> Iterator[dict[str, bool]]:
    """Change only the named local expense once, then run the real amount guard.

    The evaluator supplies isolated storage. A configured conflict must be triggered
    and cause a genuine stale-amount refusal to count as exercised. All patches are
    restored when the turn finishes or raises.
    """
    from src.bot.storage import dynamodb
    from src.bot.tools import expenses

    outcome = {"applied": False, "rejected": False}
    if conflict is None:
        yield outcome
        return
    original = expenses._load_for_change

    def load(
        requested_ledger: str, expense_id: str, expected_amount: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Inject once after a valid lookup, emulating a stale read of the amount."""
        item, error = original(requested_ledger, expense_id, expected_amount)
        if (
            not outcome["applied"]
            and requested_ledger == ledger_id
            and item is not None
            and item["summary"].casefold() == conflict.summary.casefold()
            and Decimal(str(item["amount"])) != conflict.amount
        ):
            dynamodb.update_item(item["PK"], item["SK"], {"amount": conflict.amount})
            outcome["applied"] = True
            item, error = original(requested_ledger, expense_id, expected_amount)
            outcome["rejected"] = item is None and error is not None
        return item, error

    with patch.object(expenses, "_load_for_change", side_effect=load):
        yield outcome


def _preserved_expenses(
    before: list[dict[str, Any]], after: list[dict[str, Any]], summaries: list[str]
) -> bool:
    """Compare complete records, including IDs and audit fields, for named expenses."""
    for summary in summaries:
        original = sorted(
            (
                item
                for item in before
                if item["summary"].casefold() == summary.casefold()
            ),
            key=lambda item: item["SK"],
        )
        current = sorted(
            (
                item
                for item in after
                if item["summary"].casefold() == summary.casefold()
            ),
            key=lambda item: item["SK"],
        )
        if not original or current != original:
            return False
    return True


def run_scenario(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    scenario: Scenario,
    ledger_id: str,
    fx_rates: dict[str, float],
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run real tools through shared operations and check every turn's stored outcome.

    Storage is supplied by the caller's isolated configuration. A step failure is
    recorded with its partial transcript before propagating, so paid requests remain
    reviewable. Successful trip-end cleanup happens after the reply is captured.
    """
    from src.bot.agent.graph import clear_thread_history, thread_id_for
    from src.bot.conversation import (
        NoPendingConfirmationError,
        PendingConfirmationError,
        awaiting_confirmation,
        cancel_end_trip,
        config_for,
        confirm_end_trip,
        extract_text,
        send_message,
    )
    from src.bot.storage import dynamodb
    from src.bot.tools import expenses, trip

    result: dict[str, Any] = {
        "scenario": scenario.name,
        "ledger_id": ledger_id,
        "steps": [],
    }
    config = config_for(ledger_id)
    for step in scenario.steps:
        before = graph.get_state(config)
        previous_count = len(before.values.get("messages", []))
        entry: dict[str, Any] = {
            "action": step.action,
            "text": step.text,
            "message_time": step.message_time.isoformat(),
        }
        result["steps"].append(entry)
        if step.amount_conflict is not None:
            entry["amount_conflict"] = step.amount_conflict.model_dump(mode="json")
        started = time.perf_counter()
        ended = False
        try:
            before_expenses = (
                expenses.list_expenses(ledger_id)
                if step.expected.preserve_expenses
                else []
            )
            with (
                patch.object(trip, "get_sgd_exchange_rates", return_value=fx_rates),
                patch.object(
                    expenses,
                    "_now_utc",
                    return_value=step.message_time.isoformat(timespec="microseconds"),
                ),
                patch.object(trip, "datetime", wraps=datetime) as clock,
                _amount_conflict(ledger_id, step.amount_conflict) as conflict,
            ):
                clock.now.return_value = step.message_time
                try:
                    if step.action == "message":
                        response = send_message(
                            graph,
                            ledger_id,
                            str(step.text),
                            step.message_time.isoformat(),
                            state=before,
                        )
                    elif step.action == "confirm":
                        confirmation = confirm_end_trip(graph, ledger_id, state=before)
                        response, ended = confirmation.result, confirmation.trip_ended
                        if not ended:
                            clear_thread_history(graph, thread_id_for(ledger_id))
                            raise RuntimeError(
                                "Trip remained active after confirmation"
                            )
                    else:
                        response = cancel_end_trip(graph, ledger_id, state=before)
                    new_messages = response["messages"][previous_count:]
                    entry["reply"] = extract_text(response["messages"][-1].content)
                except PendingConfirmationError as error:
                    new_messages = []
                    entry.update(reply=str(error), blocked=True)
            entry["messages"] = messages_to_dict(new_messages)
            if step.amount_conflict is not None:
                entry["amount_conflict"].update(conflict)
            # Capturing the reply is the evaluator's delivery boundary.
            if ended:
                clear_thread_history(graph, thread_id_for(ledger_id))
            state = graph.get_state(config)
            active = dynamodb.get_item(f"USER#{ledger_id}", "TRIP#ACTIVE")
            stored = expenses.list_expenses(ledger_id)
            archives = dynamodb.query_by_prefix(
                f"USER#{ledger_id}", trip.ARCHIVE_SK_PREFIX
            )
            expected = step.expected
            tools = [
                call["name"]
                for message in new_messages
                if isinstance(message, AIMessage)
                for call in message.tool_calls
            ]
            tool_results = [
                str(message.content)
                for message in new_messages
                if isinstance(message, ToolMessage)
            ]
            checks = {
                "trip_active": (active is not None) == expected.trip_active,
                "timezone": expected.timezone is None
                or (active or {}).get("timezone") == expected.timezone,
                "expenses": sorted(_expense_signature(item) for item in stored)
                == sorted(
                    _expense_signature(item.model_dump()) for item in expected.expenses
                ),
                "confirmation": awaiting_confirmation(state)
                == expected.awaiting_confirmation,
                "archive": expected.archived_expenses is None
                or sum(len(item["expenses"]) for item in archives)
                == expected.archived_expenses,
                "reply": all(
                    text.casefold() in entry["reply"].casefold()
                    for text in expected.reply_contains
                ),
                "required_tools": all(
                    tool in tools for tool in expected.required_tools
                ),
                "forbidden_tools": all(
                    tool not in tools for tool in expected.forbidden_tools
                ),
                "preserved_expenses": _preserved_expenses(
                    before_expenses, stored, expected.preserve_expenses
                ),
                "tool_results": all(
                    any(
                        text.casefold() in content.casefold()
                        for content in tool_results
                    )
                    for text in expected.tool_results_contain
                ),
                "amount_conflict": step.amount_conflict is None
                or (conflict["applied"] and conflict["rejected"]),
                "history_cleared_after_end": not ended
                or not state.values.get("messages"),
            }
            entry.update(
                checks=checks,
                passed=all(checks.values()),
                stored_expenses=stored,
                active_trip=active,
            )
        except NoPendingConfirmationError as error:
            # A model may never request end_trip. Mark this conversation as failed
            # without spending calls on its remaining steps or skipping other cases.
            entry.update(
                passed=False, error=f"{type(error).__name__}: {error}", messages=[]
            )
            break
        except Exception as error:
            partial = graph.get_state(config)
            entry.update(
                passed=False,
                error=f"{type(error).__name__}: {error}",
                messages=messages_to_dict(
                    partial.values.get("messages", [])[previous_count:]
                ),
            )
            raise
        finally:
            entry["seconds"] = round(time.perf_counter() - started, 6)
            if on_step is not None:
                on_step(result)
    result["passed"] = all(entry["passed"] for entry in result["steps"])
    return result


def _positive_decimal(raw: str) -> Decimal:
    """Parse a finite positive budget or per-million-token price."""
    try:
        number = Decimal(raw)
        if number.is_finite() and number > 0:
            return number
    except ArithmeticError:
        pass
    raise argparse.ArgumentTypeError("must be a finite positive number")


def main(argv: list[str] | None = None) -> int:
    """Print a free plan, or run an explicitly authorized evaluation in local storage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--model")
    parser.add_argument("--region")
    parser.add_argument("--profile")
    parser.add_argument("--db-endpoint")
    parser.add_argument("--temperature", type=float)
    parser.add_argument(
        "--request-fields",
        default="{}",
        help="JSON object of model-specific Converse fields",
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-cost-usd", type=_positive_decimal)
    parser.add_argument(
        "--input-price", type=_positive_decimal, help="USD per million input tokens"
    )
    parser.add_argument(
        "--output-price", type=_positive_decimal, help="USD per million output tokens"
    )
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    suite = Suite.model_validate_json(args.scenarios.read_text(encoding="utf-8"))
    selected = [
        item for item in suite.scenarios if not args.only or item.name in args.only
    ]
    if args.only and set(args.only) - {item.name for item in suite.scenarios}:
        parser.error("--only contains unknown scenario names")
    if args.runs < 1 or args.max_output_tokens < 1:
        parser.error("--runs and --max-output-tokens must be positive")
    try:
        fields = json.loads(args.request_fields)
    except json.JSONDecodeError as error:
        parser.error(f"--request-fields is not valid JSON: {error}")
    if not isinstance(fields, dict):
        parser.error("--request-fields must be a JSON object")
    plan = dict(
        scenarios=[item.name for item in selected],
        runs=args.runs,
        turns=sum(len(item.steps) for item in selected) * args.runs,
        model=args.model,
        region=args.region,
        paid_execution=args.allow_paid,
    )
    print(json.dumps(plan, indent=2))
    if not args.allow_paid:
        print("Plan only. No database or model requests made.")
        return 0
    if (
        not args.model
        or not args.region
        or not args.max_requests
        or args.max_requests < 1
        or not args.max_cost_usd
        or not args.input_price
        or not args.output_price
    ):
        parser.error(
            "Paid runs require --model, --region, --max-requests, --max-cost-usd, --input-price and --output-price"
        )
    import boto3
    from botocore.config import Config

    from scripts.compare_models import build_model
    from scripts.deploy_lambda import resolve_profile
    from src.bot.agent.graph import build_graph
    from src.bot.agent.nodes import tools
    from src.bot.config import settings

    if settings.ENVIRONMENT != "dev":
        parser.error("Evaluation requires ENVIRONMENT=dev")
    endpoint = validate_local_endpoint(
        args.db_endpoint or settings.DYNAMODB_ENDPOINT_URL
    )
    client = boto3.client(
        "dynamodb",
        region_name=settings.AWS_REGION,
        endpoint_url=endpoint,
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    run_id = uuid4().hex
    table = f"{EVALUATION_TABLE_PREFIX}{run_id}"
    output = (
        args.output
        or Path(tempfile.gettempdir()) / "expenses-model-evaluation" / f"{run_id}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    model = build_model(
        boto3.Session(profile_name=resolve_profile(args.profile)),
        args.model,
        args.temperature,
        fields,
        region=args.region,
        max_tokens=args.max_output_tokens,
        client_config=Config(
            connect_timeout=5, read_timeout=90, retries={"total_max_attempts": 1}
        ),
    )
    tool_tokens = len(json.dumps([tool.args for tool in tools]))
    metered = MeteredModel(
        model,
        args.max_requests,
        args.max_cost_usd,
        args.input_price,
        args.output_price,
        args.max_output_tokens,
        tool_tokens=tool_tokens,
    )
    report: dict[str, Any] = {
        "plan": plan,
        "request_fields": fields,
        "temperature": args.temperature,
        "fx_rates": suite.fx_rates,
        "input_price_per_million": str(args.input_price),
        "output_price_per_million": str(args.output_price),
        "max_requests": args.max_requests,
        "max_estimated_cost_usd": str(args.max_cost_usd),
        "results": [],
        "calls": metered.calls,
        "status": "running",
    }

    def save(partial: dict[str, Any] | None = None) -> None:
        """Persist completed or partial results after every attempted turn."""
        report["partial_scenario"] = partial
        report["estimated_cost_usd"] = str(metered.cost)
        output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    created = False
    exit_code = 0
    try:
        client.create_table(
            TableName=table,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        created = True
        with (
            patch.object(settings, "DYNAMODB_TABLE_NAME", table),
            patch.object(settings, "DYNAMODB_ENDPOINT_URL", endpoint),
        ):
            for repetition in range(args.runs):
                for scenario in selected:
                    result = run_scenario(
                        build_graph(model=metered),
                        scenario,
                        f"eval-{run_id}-{repetition}-{scenario.name}",
                        suite.fx_rates,
                        on_step=save,
                    )
                    result["run"] = repetition + 1
                    report["results"].append(result)
                    save()
        report["status"] = "completed"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        report["status"] = "failed"
        exit_code = 1
    finally:
        latencies = [call["seconds"] for call in metered.calls]
        report["summary"] = dict(
            requests=len(metered.calls),
            passed_scenarios=sum(item["passed"] for item in report["results"]),
            completed_scenarios=len(report["results"]),
            median_request_seconds=statistics.median(latencies) if latencies else None,
            max_request_seconds=max(latencies) if latencies else None,
            reported_input_tokens=sum(
                call.get("usage", {}).get("input_tokens", 0) for call in metered.calls
            ),
            reported_output_tokens=sum(
                call.get("usage", {}).get("output_tokens", 0) for call in metered.calls
            ),
            usage_estimated_requests=sum(
                call.get("usage_estimated", True) for call in metered.calls
            ),
        )
        try:
            save(report.get("partial_scenario"))
        finally:
            if created:
                client.delete_table(TableName=table)
    failures = sum(not item["passed"] for item in report["results"])
    print(
        f"Results: {output}\nRequests: {len(metered.calls)}; estimated USD {metered.cost}; failed scenarios: {failures}"
    )
    return exit_code or (1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(main())
