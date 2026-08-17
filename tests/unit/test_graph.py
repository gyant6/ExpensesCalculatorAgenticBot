"""Tests for graph routing logic."""

from __future__ import annotations

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from src.bot.agent.graph import custom_routes, end_trip_batch_error_node
from src.bot.agent.state import AgentState
from src.bot.tools.trip import end_trip


def _state(*messages: AnyMessage) -> AgentState:
    return AgentState(
        messages=list(messages),
        telegram_user_id="111",
        message_date="2026-01-01",
    )


def _ai(tool_names: list[str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"id_{n}", "name": n, "args": {}} for n in tool_names],
    )


# ── custom_routes ─────────────────────────────────────────────────────────────


def test_routes_to_end_when_last_message_is_human() -> None:
    from langgraph.graph import END

    assert custom_routes(_state(HumanMessage(content="hi"))) == END


def test_routes_to_end_when_ai_has_no_tool_calls() -> None:
    from langgraph.graph import END

    assert custom_routes(_state(AIMessage(content="done"))) == END


def test_routes_to_tools_for_non_end_trip_tool() -> None:
    assert custom_routes(_state(_ai(["add_expense"]))) == "tools"


def test_routes_to_end_trip_when_end_trip_is_sole_call() -> None:
    assert custom_routes(_state(_ai([end_trip.name]))) == "end_trip"


def test_routes_to_batch_error_when_end_trip_is_mixed_with_other_tools() -> None:
    assert (
        custom_routes(_state(_ai(["get_all_expenses", end_trip.name])))
        == "end_trip_batch_error"
    )


def test_routes_to_batch_error_regardless_of_end_trip_position() -> None:
    assert (
        custom_routes(_state(_ai([end_trip.name, "add_expense"])))
        == "end_trip_batch_error"
    )


# ── end_trip_batch_error_node ─────────────────────────────────────────────────


def test_batch_error_node_returns_one_tool_message_per_call() -> None:
    state = _state(_ai(["get_all_expenses", end_trip.name]))
    result = end_trip_batch_error_node(state)
    messages = result["messages"]
    assert len(messages) == 2
    assert all(isinstance(m, ToolMessage) for m in messages)


def test_batch_error_node_sets_correct_tool_call_ids() -> None:
    state = _state(_ai(["get_all_expenses", end_trip.name]))
    result = end_trip_batch_error_node(state)
    ids = {m.tool_call_id for m in result["messages"]}
    assert ids == {"id_get_all_expenses", f"id_{end_trip.name}"}


def test_batch_error_node_message_content_mentions_end_trip() -> None:
    state = _state(_ai(["get_all_expenses", end_trip.name]))
    result = end_trip_batch_error_node(state)
    for m in result["messages"]:
        assert "end_trip" in m.content
