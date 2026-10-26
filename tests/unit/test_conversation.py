"""Exercise shared operations with the real graph/tools and mocked AWS storage/models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from mypy_boto3_dynamodb import DynamoDBClient

import dev_runner
from src.bot.agent import nodes
from src.bot.agent.graph import build_graph
from src.bot.agent.state import AgentState
from src.bot.conversation import (
    NoPendingConfirmationError,
    PendingConfirmationError,
    awaiting_confirmation,
    cancel_end_trip,
    config_for,
    confirm_end_trip,
    render_confirmed_trip,
    send_message,
)
from src.bot.storage import dynamodb
from src.bot.tools import trip

MESSAGE_TIME = "2026-10-09T02:00:00+00:00"
Reply = AIMessage | Callable[[list[BaseMessage]], AIMessage]


class ScriptedModel:
    """Return supplied responses without any network or inference calls."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        """Consume the next reply, optionally inspecting prior real tool results."""
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


def tool_call(name: str, **args: Any) -> AIMessage:
    """Build a provider-compatible scripted tool call."""
    return AIMessage(
        "", tool_calls=[{"name": name, "args": args, "id": uuid4().hex[:9]}]
    )


def test_injected_model_does_not_initialize_a_default_aws_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nodes, "llm_with_tools", None)
    client = MagicMock(
        side_effect=AssertionError("Default AWS client must not be initialized")
    )
    monkeypatch.setattr("src.bot.agent.nodes.boto3.client", client)
    state = AgentState(
        messages=[],
        ledger_id="fictional",
        message_time=MESSAGE_TIME,
        trip_start_date=None,
        trip_timezone="Asia/Singapore",
        local_date="2026-10-09",
    )
    response = nodes.agent_node(state, model=ScriptedModel(AIMessage("Hello")))
    assert response["messages"][0].content == "Hello"
    client.assert_not_called()
    assert nodes.llm_with_tools is None


def test_message_uses_original_timestamp_and_persisted_history(
    dynamodb_table: DynamoDBClient,
) -> None:
    graph = build_graph(
        model=ScriptedModel(
            tool_call("start_trip", timezone="America/New_York"), AIMessage("Started")
        )
    )
    send_message(graph, "test-ledger", "Start in New York", MESSAGE_TIME)
    trip_item = dynamodb.get_item("USER#test-ledger", "TRIP#ACTIVE")
    assert trip_item is not None and trip_item["start_date"] == "2026-10-08"
    # A separately constructed graph must load the same versioned checkpoint.
    restored = build_graph(model=ScriptedModel()).get_state(config_for("test-ledger"))
    assert restored.values["message_time"] == MESSAGE_TIME
    assert len(restored.values["messages"]) == 4


def test_delete_validation_error_reaches_agent_for_retry(
    dynamodb_table: DynamoDBClient,
) -> None:
    key = ("USER#test-ledger", "EXPENSE#k7qm")
    dynamodb.put_item({"PK": key[0], "SK": "TRIP#ACTIVE", "start_date": "2026-10-09"})
    original = {
        "PK": key[0],
        "SK": key[1],
        "summary": "Coffee",
        "amount": "5500",
        "currency": "KRW",
    }
    dynamodb.put_item(original)

    def retry_after_error(messages: list[BaseMessage]) -> AIMessage:
        error = messages[-1]
        assert isinstance(error, ToolMessage)
        assert "expected_amount" in str(error.content)
        assert dynamodb.get_item(*key) == original
        return tool_call("delete_expense", expense_id="k7qm", expected_amount="5500")

    model = ScriptedModel(
        tool_call("delete_expense", expense_id="k7qm", expected_amount="5500 KRW"),
        retry_after_error,
        AIMessage("Deleted"),
    )
    result = send_message(
        graph=build_graph(model=model),
        ledger_id="test-ledger",
        text="Delete the coffee",
        message_time=MESSAGE_TIME,
    )

    assert result["messages"][-1].content == "Deleted"
    assert dynamodb.get_item(*key) is None
    assert not model.replies


def test_confirmation_blocks_new_messages_and_cancellation_keeps_trip(
    dynamodb_table: DynamoDBClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dynamodb.put_item(
        {"PK": "USER#test-ledger", "SK": "TRIP#ACTIVE", "start_date": "2026-10-09"}
    )
    model = ScriptedModel(tool_call("end_trip"), AIMessage("Cancelled"))
    graph = build_graph(model=model)
    fx = MagicMock(side_effect=AssertionError("Cancellation must not fetch rates"))
    monkeypatch.setattr(trip, "get_sgd_exchange_rates", fx)
    send_message(graph, "test-ledger", "End trip", MESSAGE_TIME)
    paused = graph.get_state(config_for("test-ledger"))
    assert awaiting_confirmation(paused)
    with pytest.raises(PendingConfirmationError):
        send_message(graph, "test-ledger", "Lunch 12", MESSAGE_TIME)
    assert len(model.replies) == 1
    result = cancel_end_trip(graph, "test-ledger")
    assert result["messages"][-1].content == "Cancelled"
    assert dynamodb.get_item("USER#test-ledger", "TRIP#ACTIVE") is not None
    assert not awaiting_confirmation(graph.get_state(config_for("test-ledger")))
    assert graph.get_state(config_for("test-ledger")).values["messages"]
    fx.assert_not_called()
    with pytest.raises(NoPendingConfirmationError):
        confirm_end_trip(graph, "test-ledger")


def test_mixed_end_trip_batch_cannot_bypass_confirmation(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item(
        {"PK": "USER#test-ledger", "SK": "TRIP#ACTIVE", "start_date": "2026-10-09"}
    )
    mixed = AIMessage(
        "",
        tool_calls=[
            {"id": "abcdefghi", "name": "end_trip", "args": {}},
            {"id": "jklmnopqr", "name": "get_all_expenses", "args": {}},
        ],
    )
    model = ScriptedModel(mixed, tool_call("end_trip"))
    graph = build_graph(model=model)
    send_message(graph, "test-ledger", "End trip", MESSAGE_TIME)
    assert dynamodb.get_item("USER#test-ledger", "TRIP#ACTIVE") is not None
    assert awaiting_confirmation(graph.get_state(config_for("test-ledger")))
    confirmed = confirm_end_trip(graph, "test-ledger")
    assert confirmed.trip_ended
    # The graph leaves the tool result for the caller to render and deliver.
    last_message = graph.get_state(config_for("test-ledger")).values["messages"][-1]
    assert isinstance(last_message, ToolMessage)
    assert render_confirmed_trip(confirmed)[0] == (
        "Trip ended. No expenses were recorded. 🐾"
    )
    assert not model.replies


def test_dev_runner_does_not_report_a_no_op_resume_as_success(
    dynamodb_table: DynamoDBClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dynamodb.put_item(
        {"PK": "USER#test-ledger", "SK": "TRIP#ACTIVE", "start_date": "2026-10-09"}
    )
    graph = MagicMock()
    graph.get_state.return_value = MagicMock(next=("end_trip_node",))
    graph.invoke.return_value = {"messages": [AIMessage("Incorrect success summary")]}
    monkeypatch.setattr(dev_runner, "build_graph", lambda: graph)
    answers = iter(["yes", "exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    clear = MagicMock()
    monkeypatch.setattr(dev_runner, "clear_thread_history", clear)
    dev_runner.run("test-ledger")
    output = capsys.readouterr().out
    assert "Could not end the trip" in output
    assert "Incorrect success summary" not in output
    assert dynamodb.get_item("USER#test-ledger", "TRIP#ACTIVE") is not None
    clear.assert_called_once()


def test_confirmation_verification_requests_a_strongly_consistent_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.get_item.return_value = {}
    monkeypatch.setattr(dynamodb, "get_client", lambda: client)
    graph = MagicMock()
    graph.get_state.return_value = MagicMock(next=("end_trip_node",))
    graph.invoke.return_value = {
        "messages": [
            ToolMessage(
                "Trip successfully ended.\n\ndate,summary,category,amount,currency,amount_sgd,payment_method\r\n",
                tool_call_id="t",
                name="end_trip",
            ),
            AIMessage("Ended"),
        ]
    }
    assert confirm_end_trip(graph, "fictional").trip_ended
    assert client.get_item.call_args.kwargs["ConsistentRead"] is True


def test_confirmation_ignores_a_stale_success_in_prior_history() -> None:
    old_success = ToolMessage(
        "Trip successfully ended.\n\ndate,summary,category,amount,currency,amount_sgd,payment_method\r\n",
        tool_call_id="old",
        name="end_trip",
    )
    graph = MagicMock()
    graph.get_state.return_value = MagicMock(
        next=("end_trip_node",), values={"messages": [old_success]}
    )
    graph.invoke.return_value = {"messages": [old_success, AIMessage("Ended")]}
    read = MagicMock(return_value=None)

    confirmation = confirm_end_trip(graph, "fictional", trip_reader=read)

    assert not confirmation.trip_ended
    assert confirmation.csv_bytes is None
    read.assert_not_called()
