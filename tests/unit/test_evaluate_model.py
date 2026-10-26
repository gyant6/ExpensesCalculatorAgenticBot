"""Verify evaluator correctness, isolation, reporting and pre-inference budget limits."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from mypy_boto3_dynamodb import DynamoDBClient

from scripts import evaluate_model as evaluation
from src.bot.agent.graph import build_graph
from src.bot.config import settings
from src.bot.storage import dynamodb
from tests.unit.test_conversation import ScriptedModel, tool_call


def _suite() -> evaluation.Suite:
    return evaluation.Suite.model_validate_json(
        evaluation.DEFAULT_SCENARIOS.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Total so far: ¥33,210 JPY", True),
        ("Total so far: ¥31,210 JPY", False),
        ("Total: 33,210 JPY", True),
        ("The expenses are listed above.", True),
    ],
)
def test_reported_total_matches_stored_expenses(reply: str, expected: bool) -> None:
    expenses = [
        {"amount": "11900", "currency": "JPY"},
        {"amount": "21310", "currency": "JPY"},
    ]
    assert evaluation._reported_total_matches(reply, expenses) is expected


def _lunch_replies() -> list[AIMessage]:
    return [
        tool_call("start_trip", timezone="Asia/Tokyo"),
        AIMessage("Started"),
        tool_call(
            "add_expense",
            summary="Lunch",
            amount="1200",
            currency="JPY",
            category="Food",
            payment_method="Cash",
            source_message="Lunch 1200 yen cash",
        ),
        AIMessage("Recorded"),
    ]


@pytest.mark.parametrize("name", ["confirm_trip_end", "cancel_trip_end"])
def test_complete_conversation_executes_real_tools_with_fixed_fx_and_time(
    dynamodb_table: DynamoDBClient,
    name: str,
) -> None:
    suite = _suite()
    scenario = next(item for item in suite.scenarios if item.name == name)
    model = ScriptedModel(
        *_lunch_replies(),
        tool_call("end_trip"),
        *([AIMessage("Cancelled")] if name == "cancel_trip_end" else []),
    )
    result = evaluation.run_scenario(
        build_graph(model=model), scenario, "fictional-eval-ledger", suite.fx_rates
    )
    assert result["passed"], result
    assert not model.replies
    first_expense = result["steps"][1]["stored_expenses"][0]
    assert first_expense["created_at"] == "2026-10-09T03:01:00.000000+00:00"
    if name == "confirm_trip_end":
        archive = dynamodb.query_by_prefix("USER#fictional-eval-ledger", "ARCHIVE#")
        assert len(archive) == 1 and len(archive[0]["expenses"]) == 1
        assert "Total: SGD 12.00" in result["steps"][-1]["reply"]
        last_messages = result["steps"][-1]["messages"]
        assert any(
            "12.00" in str(message["data"]["content"]) for message in last_messages
        )
    else:
        blocked = result["steps"][3]
        assert blocked["blocked"] and blocked["messages"] == []


def test_targeted_edits_preserve_the_unrelated_expense(
    dynamodb_table: DynamoDBClient,
) -> None:
    suite = _suite()
    scenario = next(
        item for item in suite.scenarios if item.name == "targeted_category_edit"
    )
    additions = AIMessage(
        "",
        tool_calls=[
            {
                "id": f"add{index:06}",
                "name": "add_expense",
                "args": dict(
                    summary=summary,
                    amount=amount,
                    currency="JPY",
                    category=category,
                    source_message=summary,
                ),
            }
            for index, (summary, amount, category) in enumerate(
                [
                    ("Notebook", "500", "Shopping"),
                    ("Pen", "200", "Shopping"),
                    ("Train", "300", "Transport"),
                ]
            )
        ],
    )

    def edit_from_real_ids(messages: list[BaseMessage]) -> AIMessage:
        """Use IDs returned by the actual add tools instead of fixture IDs."""
        results = {
            message.tool_call_id: str(message.content)
            .removeprefix("Expense recorded with id ")
            .removesuffix(".")
            for message in messages
            if isinstance(message, ToolMessage) and message.name == "add_expense"
        }
        return AIMessage(
            "",
            tool_calls=[
                {
                    "id": f"edit{index:05}",
                    "name": "edit_expense",
                    "args": dict(
                        expense_id=results[str(call["id"])],
                        expected_amount=call["args"]["amount"],
                        category="Misc",
                        edit_message="Change to Misc",
                    ),
                }
                for index, call in enumerate(additions.tool_calls[:2])
            ],
        )

    model = ScriptedModel(
        tool_call("start_trip", timezone="Asia/Tokyo"),
        AIMessage("Started"),
        additions,
        AIMessage("Recorded"),
        edit_from_real_ids,
        AIMessage("Edited"),
    )
    result = evaluation.run_scenario(
        build_graph(model=model), scenario, "fictional-eval-ledger", suite.fx_rates
    )
    assert result["passed"], result
    train_before = next(
        item
        for item in result["steps"][1]["stored_expenses"]
        if item["summary"] == "Train"
    )
    train_after = next(
        item
        for item in result["steps"][2]["stored_expenses"]
        if item["summary"] == "Train"
    )
    assert train_after == train_before


@pytest.mark.parametrize(
    "name",
    [
        "multiple_expenses",
        "timezone_midnight",
        "relative_dates",
        "destination_clarification",
    ],
)
def test_remaining_fixtures_against_scripted_models_and_real_tools(
    dynamodb_table: DynamoDBClient,
    name: str,
) -> None:
    suite = _suite()
    scenario = next(item for item in suite.scenarios if item.name == name)
    if name == "destination_clarification":
        replies = [
            AIMessage("Where are you heading?"),
            tool_call("start_trip", timezone="Asia/Taipei"),
            AIMessage("Started"),
        ]
    elif name == "timezone_midnight":
        replies = [
            tool_call("start_trip", timezone="America/New_York"),
            AIMessage("Started"),
            tool_call(
                "add_expense",
                summary="Dinner",
                amount="20",
                currency="USD",
                category="Food",
                source_message="Dinner 20 USD",
            ),
            AIMessage("Recorded"),
            tool_call("set_trip_timezone", timezone="Asia/Tokyo"),
            AIMessage("Time zone changed"),
            tool_call(
                "add_expense",
                summary="Coffee",
                amount="400",
                currency="JPY",
                category="Food",
                source_message="Coffee 400 yen",
            ),
            AIMessage("Recorded"),
        ]
    else:
        zone = "Asia/Tokyo" if name == "multiple_expenses" else "Asia/Seoul"
        if name == "multiple_expenses":
            batch = [
                dict(
                    summary="Lunch",
                    amount="1200",
                    currency="JPY",
                    category="Food",
                    source_message="Lunch 1200 yen cash",
                    payment_method="Cash",
                ),
                dict(
                    summary="Train",
                    amount="300",
                    currency="JPY",
                    category="Transport",
                    source_message="Train 300 yen",
                ),
            ]
        else:
            batch = [
                dict(
                    summary="Dinner",
                    amount="12000",
                    currency="KRW",
                    category="Food",
                    source_message="Dinner on Tuesday",
                    payment_method="Cash",
                    date="2026-10-06",
                ),
                dict(
                    summary="Lunch",
                    amount="8000",
                    currency="KRW",
                    category="Food",
                    source_message="Lunch yesterday",
                    payment_method="Cash",
                    date="2026-10-08",
                ),
            ]
        additions = AIMessage(
            "",
            tool_calls=[
                {"id": f"add{index:06}", "name": "add_expense", "args": args}
                for index, args in enumerate(batch)
            ],
        )
        replies = [
            tool_call("start_trip", timezone=zone),
            AIMessage("Started"),
            additions,
            AIMessage("Recorded"),
        ]
        if name == "multiple_expenses":
            replies.extend(
                [tool_call("get_all_expenses"), AIMessage("Here are your expenses")]
            )
    model = ScriptedModel(*replies)
    result = evaluation.run_scenario(
        build_graph(model=model), scenario, "fictional-eval-ledger", suite.fx_rates
    )
    assert result["passed"], result
    assert not model.replies


def test_checker_fails_when_an_unrelated_expense_changes(
    dynamodb_table: DynamoDBClient,
) -> None:
    scenario = evaluation.Scenario(
        name="unintended_write",
        steps=[
            evaluation.Step(
                text="Hello",
                message_time="2026-10-09T03:00:00Z",
                expected=evaluation.Expectation(trip_active=False),
            )
        ],
    )
    model = ScriptedModel(
        tool_call(
            "add_expense",
            summary="Unexpected",
            amount="12",
            currency="SGD",
            category="Food",
            source_message="Unexpected",
        ),
        AIMessage("Hello"),
    )
    result = evaluation.run_scenario(
        build_graph(model=model), scenario, "fictional-eval-ledger", {"SGD": 1}
    )
    assert not result["passed"]
    assert not result["steps"][0]["checks"]["expenses"]


def test_missing_confirmation_fails_only_its_scenario(
    dynamodb_table: DynamoDBClient,
) -> None:
    suite = _suite()
    scenario = next(item for item in suite.scenarios if item.name == "confirm_trip_end")
    model = ScriptedModel(*_lunch_replies(), AIMessage("Which trip?"))
    partials: list[dict[str, Any]] = []
    result = evaluation.run_scenario(
        build_graph(model=model),
        scenario,
        "failed-ledger",
        suite.fx_rates,
        on_step=partials.append,
    )
    assert not result["passed"]
    assert "NoPendingConfirmationError" in result["steps"][-1]["error"]
    assert len(result["steps"]) == 4 and len(partials) == 4
    # A failure in one ledger must not keep the following conversation from running.
    following = next(
        item for item in suite.scenarios if item.name == "destination_clarification"
    )
    following_model = ScriptedModel(
        AIMessage("Where are you heading?"),
        tool_call("start_trip", timezone="Asia/Taipei"),
        AIMessage("Started"),
    )
    assert evaluation.run_scenario(
        build_graph(model=following_model), following, "next-ledger", suite.fx_rates
    )["passed"]


def _meter(model: ScriptedModel, **overrides: Any) -> evaluation.MeteredModel:
    params: dict[str, Any] = dict(
        max_requests=1,
        max_cost=Decimal("1"),
        input_price=Decimal("1"),
        output_price=Decimal("1"),
        max_output_tokens=100,
    )
    params.update(overrides)
    return evaluation.MeteredModel(model, **params)


def test_request_limit_blocks_the_next_call_before_inference() -> None:
    model = ScriptedModel(AIMessage("First"), AIMessage("Must not run"))
    metered = _meter(model)
    metered.invoke([])
    with pytest.raises(evaluation.BudgetExceeded):
        metered.invoke([])
    assert len(model.replies) == 1
    assert len(metered.calls) == 1


def test_estimated_cost_limit_blocks_the_first_call_before_inference() -> None:
    model = ScriptedModel(AIMessage("Must not run"))
    metered = _meter(model, max_cost=Decimal("0.000001"))
    with pytest.raises(evaluation.BudgetExceeded):
        metered.invoke([])
    assert len(model.replies) == 1
    assert not metered.calls


def test_report_records_usage_and_charges_failed_calls_conservatively() -> None:
    response = AIMessage(
        "Reply",
        usage_metadata={"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
    )
    metered = _meter(ScriptedModel(response))
    metered.invoke([])
    assert metered.cost == Decimal("0.000023")
    assert metered.calls[0]["usage"]["input_tokens"] == 20
    assert not metered.calls[0]["usage_estimated"]
    assert metered.calls[0]["seconds"] >= 0
    failed = _meter(ScriptedModel())
    with pytest.raises(IndexError):
        failed.invoke([])
    assert failed.cost > 0
    assert len(failed.calls) == 1 and "error" in failed.calls[0]


def test_default_cli_prints_a_free_plan_without_clients(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "boto3.client", MagicMock(side_effect=AssertionError("No clients allowed"))
    )
    monkeypatch.setattr(
        "boto3.Session", MagicMock(side_effect=AssertionError("No sessions allowed"))
    )
    assert evaluation.main([]) == 0
    assert "No database or model requests made" in capsys.readouterr().out


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "https://dynamodb.ap-southeast-1.amazonaws.com",
        "http://example.com:8000",
        "http://localhost.example.com:8000",
        "http://user:pass@localhost:8000",
    ],
)
def test_storage_refuses_any_nonlocal_target(endpoint: str | None) -> None:
    with pytest.raises(ValueError):
        evaluation.validate_local_endpoint(endpoint)


def test_cli_keeps_partial_report_and_deletes_only_its_own_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original_table = settings.DYNAMODB_TABLE_NAME
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    client = MagicMock()
    monkeypatch.setattr("boto3.client", MagicMock(return_value=client))
    monkeypatch.setattr("boto3.Session", MagicMock())
    model_factory = MagicMock(return_value=ScriptedModel())
    monkeypatch.setattr("scripts.compare_models.build_model", model_factory)
    monkeypatch.setattr("src.bot.agent.graph.build_graph", MagicMock())

    def failed_scenario(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """Fail after recording a turn, while checking the isolated settings."""
        assert settings.DYNAMODB_TABLE_NAME.startswith(
            evaluation.EVALUATION_TABLE_PREFIX
        )
        assert settings.DYNAMODB_TABLE_NAME != original_table
        kwargs["on_step"]({"scenario": "partial", "steps": [{"passed": False}]})
        raise RuntimeError("Simulated interruption")

    monkeypatch.setattr(evaluation, "run_scenario", failed_scenario)
    output = tmp_path / "report.json"
    assert (
        evaluation.main(
            [
                "--allow-paid",
                "--model",
                "fictional-model",
                "--region",
                "us-east-1",
                "--max-requests",
                "2",
                "--max-cost-usd",
                "1",
                "--input-price",
                "1",
                "--output-price",
                "1",
                "--db-endpoint",
                "http://localhost:8000",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert "Simulated interruption" in report["error"]
    assert report["partial_scenario"]["steps"] == [{"passed": False}]
    table = client.create_table.call_args.kwargs["TableName"]
    assert table.startswith(evaluation.EVALUATION_TABLE_PREFIX)
    client.delete_table.assert_called_once_with(TableName=table)
    assert settings.DYNAMODB_TABLE_NAME == original_table
    assert model_factory.call_args.kwargs["client_config"].retries == {
        "total_max_attempts": 1
    }
