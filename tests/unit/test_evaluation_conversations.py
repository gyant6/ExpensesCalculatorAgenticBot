"""Exercise expanded fixtures with scripted decisions and real graph/storage tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from mypy_boto3_dynamodb import DynamoDBClient

from scripts import evaluate_model as evaluation
from src.bot.agent import nodes
from src.bot.agent.graph import build_graph
from src.bot.config import settings
from src.bot.conversation import config_for
from src.bot.tools import expenses
from tests.unit.test_conversation import Reply, ScriptedModel, tool_call


def _scenario(name: str) -> tuple[evaluation.Suite, evaluation.Scenario]:
    """Load a public fixture independently of the scripted model decisions below."""
    suite = evaluation.Suite.model_validate_json(
        evaluation.DEFAULT_SCENARIOS.read_text(encoding="utf-8")
    )
    return suite, next(item for item in suite.scenarios if item.name == name)


def _add(
    summary: str, amount: str, category: str = "Food", payment: str = "Card"
) -> AIMessage:
    """Construct an addition with independent values, never from expected outcomes."""
    return tool_call(
        "add_expense",
        summary=summary,
        amount=amount,
        currency="JPY",
        category=category,
        payment_method=payment,
        source_message=summary,
    )


def _batch(*responses: AIMessage) -> AIMessage:
    """Combine independently scripted additions into one model response."""
    return AIMessage(
        "", tool_calls=[call for response in responses for call in response.tool_calls]
    )


def _latest_list(messages: list[BaseMessage]) -> list[list[str]]:
    """Read only the latest real get_all_expenses result visible to the model."""
    result = next(
        message
        for message in reversed(messages)
        if isinstance(message, ToolMessage) and message.name == "get_all_expenses"
    )
    return [
        [field.strip() for field in line.split("|")]
        for line in str(result.content).splitlines()[1:]
    ]


def _change(
    summary: str, operation: str = "edit_expense", **changes: Any
) -> Callable[[list[BaseMessage]], AIMessage]:
    """Resolve a real ID and current amount from the latest model-visible listing."""

    def respond(messages: list[BaseMessage]) -> AIMessage:
        """Make one change using the actual tool result rather than fixture IDs."""
        row = next(row for row in _latest_list(messages) if row[2] == summary)
        args = dict(expense_id=row[1], expected_amount=row[4].split()[0], **changes)
        if operation == "edit_expense":
            args["edit_message"] = "Correct the requested fields"
        return tool_call(operation, **args)

    return respond


def _list_reply(messages: list[BaseMessage]) -> AIMessage:
    """Show a realistic full list, omitting the IDs intended only for tool calls."""
    return AIMessage(
        "\n".join(f"{row[0]}. " + " | ".join(row[2:]) for row in _latest_list(messages))
    )


def _listing() -> list[Reply]:
    """Fetch the actual stored expenses and render them for the user."""
    return [tool_call("get_all_expenses"), _list_reply]


def _start() -> list[Reply]:
    """Start the fictional Tokyo trip through the real start tool."""
    return [tool_call("start_trip", timezone="Asia/Tokyo"), AIMessage("Started")]


def _deletion_setup() -> list[Reply]:
    """Two similarly named, equal-priced expenses make a wrong-ID deletion detectable."""
    return [
        *_start(),
        _batch(
            _add("Airport train", "1800", "Transport"),
            _add("City train", "1800", "Transport"),
            _add("Tea", "300", payment="Cash"),
        ),
        AIMessage("Recorded"),
    ]


def test_targeted_deletion_preserves_other_complete_records(
    dynamodb_table: DynamoDBClient,
) -> None:
    suite, scenario = _scenario("targeted_delete")
    model = ScriptedModel(
        *_deletion_setup(),
        tool_call("get_all_expenses"),
        _change("City train", "delete_expense"),
        AIMessage("Deleted"),
        *_listing(),
    )
    result = evaluation.run_scenario(
        build_graph(model=model), scenario, "delete-ledger", suite.fx_rates
    )
    assert result["passed"], result
    assert not model.replies
    assert {item["summary"] for item in result["steps"][-1]["stored_expenses"]} == {
        "Airport train",
        "Tea",
    }


def test_amount_date_and_payment_corrections_preserve_other_records(
    dynamodb_table: DynamoDBClient,
) -> None:
    suite, scenario = _scenario("expense_corrections")
    replies: list[Reply] = [
        *_start(),
        _batch(
            _add("Taxi", "2400", "Transport"),
            _add("Dinner", "3200"),
            _add("Water", "150", payment="Cash"),
        ),
        AIMessage("Recorded"),
    ]
    for summary, changes in [
        ("Taxi", {"amount": "2600"}),
        ("Taxi", {"date": "2026-10-08"}),
        ("Dinner", {"payment_method": "Cash"}),
    ]:
        replies.extend(
            [
                tool_call("get_all_expenses"),
                _change(summary, **changes),
                AIMessage("Updated"),
            ]
        )
    replies.extend(_listing())
    model = ScriptedModel(*replies)
    result = evaluation.run_scenario(
        build_graph(model=model), scenario, "corrections-ledger", suite.fx_rates
    )
    assert result["passed"], result
    assert not model.replies
    taxi_before = next(
        item
        for item in result["steps"][1]["stored_expenses"]
        if item["summary"] == "Taxi"
    )
    taxi_after = next(
        item
        for item in result["steps"][-1]["stored_expenses"]
        if item["summary"] == "Taxi"
    )
    assert taxi_after["SK"] == taxi_before["SK"]
    assert taxi_after["created_at"] == taxi_before["created_at"]


def _recovery_replies(recover: bool) -> list[Reply]:
    """Try the now-stale amount once, then optionally re-read before retrying."""
    replies: list[Reply] = [
        *_start(),
        _batch(
            _add("Lunch", "1200"),
            _add("Train", "300", "Transport"),
        ),
        AIMessage("Recorded"),
        tool_call("get_all_expenses"),
        _change("Lunch", payment_method="Cash"),
    ]
    if recover:
        replies.extend(
            [tool_call("get_all_expenses"), _change("Lunch", payment_method="Cash")]
        )
    replies.extend([AIMessage("Updated"), *_listing()])
    return replies


@pytest.mark.parametrize("recover", [True, False])
def test_stale_amount_requires_real_refusal_and_successful_recovery(
    dynamodb_table: DynamoDBClient,
    recover: bool,
) -> None:
    suite, scenario = _scenario("stale_amount_recovery")
    original_load = expenses._load_for_change
    model = ScriptedModel(*_recovery_replies(recover))
    result = evaluation.run_scenario(
        build_graph(model=model), scenario, "recovery-ledger", suite.fx_rates
    )
    assert result["passed"] is recover, result
    assert result["steps"][2]["checks"]["amount_conflict"]
    if not recover:
        assert not result["steps"][2]["checks"]["expenses"]
        assert not result["steps"][2]["checks"]["tool_results"]
    assert expenses._load_for_change is original_load
    assert not model.replies


def test_preservation_check_catches_an_unrequested_audit_only_edit(
    dynamodb_table: DynamoDBClient,
) -> None:
    suite, scenario = _scenario("targeted_delete")
    model = ScriptedModel(
        *_deletion_setup(),
        tool_call("get_all_expenses"),
        _change("City train", "delete_expense"),
        _change("Tea", payment_method="Cash"),
        AIMessage("Deleted"),
        *_listing(),
    )
    result = evaluation.run_scenario(
        build_graph(model=model), scenario, "unrelated-ledger", suite.fx_rates
    )
    assert not result["passed"]
    # User-visible fields are still right; the source message and timestamp were changed.
    assert result["steps"][2]["checks"]["expenses"]
    assert not result["steps"][2]["checks"]["preserved_expenses"]


def test_long_conversation_recovers_early_expenses_after_real_history_trimming(
    dynamodb_table: DynamoDBClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite, scenario = _scenario("long_conversation")
    items = [
        ("Airport bus", "1400", "Transport"),
        ("Breakfast", "650", "Food"),
        ("Metro", "250", "Transport"),
        ("Museum ticket", "1800", "Leisure"),
        ("Lunch", "950", "Food"),
        ("Coffee", "420", "Food"),
        ("Hotel", "12000", "Accommodation"),
        ("Dinner", "2300", "Food"),
        ("Water", "160", "Food"),
        ("Train", "1100", "Transport"),
        ("Souvenir", "2400", "Shopping"),
        ("Bakery", "580", "Food"),
        ("Taxi", "3100", "Transport"),
        ("Pharmacy", "1700", "Shopping"),
        ("Park ticket", "900", "Leisure"),
        ("Ferry", "1600", "Transport"),
        ("Book", "1350", "Shopping"),
        ("Laundry", "700", "Misc"),
    ]
    replies = _start()
    for index, (summary, amount, category) in enumerate(items, 1):
        replies.extend([_add(summary, amount, category), AIMessage("Recorded")])
        if index % 3 == 0:
            replies.extend(_listing())
    for summary, changes in [
        ("Hotel", {"amount": "11900"}),
        ("Souvenir", {"category": "Misc"}),
        ("Pharmacy", {"payment_method": "Cash"}),
        ("Laundry", {"date": "2026-10-08"}),
        ("Ferry", {"amount": "1700"}),
    ]:
        replies.extend(
            [
                tool_call("get_all_expenses"),
                _change(summary, **changes),
                AIMessage("Updated"),
                *_listing(),
            ]
        )

    def edit_early_expense(messages: list[BaseMessage]) -> AIMessage:
        """Retrieve the first expense after its original message leaves the window."""
        assert not any(
            isinstance(message, HumanMessage)
            and message.content == "Airport bus 1400 yen by card"
            for message in messages
        )
        return _change("Airport bus", amount="1500")(messages)

    for summary, operation, changes in [
        ("Airport bus", "edit_expense", {"amount": "1500"}),
        ("Breakfast", "edit_expense", {"date": "2026-10-08"}),
        ("Metro", "delete_expense", {}),
        ("Coffee", "edit_expense", {"payment_method": "Cash"}),
    ]:
        replies.extend(
            [
                tool_call("get_all_expenses"),
                edit_early_expense
                if summary == "Airport bus"
                else _change(summary, operation, **changes),
                AIMessage("Updated"),
            ]
        )
    replies.extend(_listing())
    windows: list[tuple[int, int, int]] = []
    original_bound = nodes.bounded_history

    def observe(messages: list[BaseMessage], budget: int) -> list[BaseMessage]:
        """Observe the actual production window without lowering its token budget."""
        window = original_bound(messages, budget)
        windows.append(
            (len(messages), len(window), int(count_tokens_approximately(messages)))
        )
        return window

    assert settings.MODEL_HISTORY_TOKEN_BUDGET == 8_000
    monkeypatch.setattr(nodes, "bounded_history", observe)
    model = ScriptedModel(*replies)
    graph = build_graph(model=model)
    result = evaluation.run_scenario(graph, scenario, "long-ledger", suite.fx_rates)
    assert result["passed"], result
    assert not model.replies
    assert len(result["steps"]) == 40
    assert len(result["steps"][-1]["stored_expenses"]) == 17
    persisted = build_graph(model=ScriptedModel()).get_state(config_for("long-ledger"))
    assert (
        sum(
            isinstance(message, HumanMessage)
            for message in persisted.values["messages"]
        )
        == 40
    )
    assert max(tokens for _, _, tokens in windows) > 8_000, windows[-1]
    assert any(kept < full for full, kept, _ in windows), windows[-1]
