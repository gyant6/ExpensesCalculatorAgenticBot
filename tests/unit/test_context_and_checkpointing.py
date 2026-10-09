"""Tests for bounding the history sent to the model, and saving once per turn.

A September 2026 trip averaged ~34K input tokens per model call, peaking at 90K, because
every call resent the whole trip's conversation. And the checkpointer saved a snapshot
after every graph step and kept them all: one message with a tool call wrote 7 snapshots
and 41 chunk and write items, and ending a long trip spent 14.8 s deleting them.

The checkpoint tests run on the real checkpointer against moto, because a resume broke
once already when stored state did not look the way the code expected — and because
DynamoDBSaver.prune, the first approach, turned out to raise NotImplementedError.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig

from src.bot.agent import graph as graph_module
from src.bot.agent import nodes
from src.bot.agent.graph import CHECKPOINT_DURABILITY, END_TRIP_NODE
from src.bot.agent.nodes import bounded_history
from src.bot.config import settings
from src.bot.storage import dynamodb

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

_MESSAGE_TIME = "2026-10-09T07:00:00+00:00"


def _exchange(i: int) -> list[AnyMessage]:
    """One past user turn: a message, a tool call, its result, and a reply."""
    return [
        HumanMessage(f"expense number {i}: " + "words " * 40),
        AIMessage("", tool_calls=[{"id": f"c{i}", "name": "add_expense", "args": {}}]),
        ToolMessage("Expense recorded with id abcd.", tool_call_id=f"c{i}"),
        AIMessage(f"Logged expense {i}! " + "chatter " * 20),
    ]


def _history(turns: int) -> list[AnyMessage]:
    return [m for i in range(turns) for m in _exchange(i)]


# ── bounded_history ──────────────────────────────────────────────────────────


def test_a_short_history_is_sent_whole() -> None:
    history = _history(2)
    assert bounded_history(history, 8_000) == history


def test_a_long_history_is_cut_to_the_budget_at_a_user_message() -> None:
    history = _history(200)

    window = bounded_history(history, 2_000)

    assert isinstance(window[0], HumanMessage)
    assert count_tokens_approximately(window) <= 2_000
    assert window == history[-len(window) :]
    assert len(window) < len(history)


def test_a_tool_result_is_never_separated_from_its_call() -> None:
    window = bounded_history(_history(200), 2_000)

    call_ids = {
        call["id"] for m in window if isinstance(m, AIMessage) for call in m.tool_calls
    }
    result_ids = {m.tool_call_id for m in window if isinstance(m, ToolMessage)}
    assert result_ids <= call_ids


def test_the_current_turn_is_kept_whole_even_past_the_budget() -> None:
    # The model needs its own tool results to finish the turn it is in.
    current: list[AnyMessage] = [
        HumanMessage("show all"),
        AIMessage("", tool_calls=[{"id": "g", "name": "get_all_expenses", "args": {}}]),
        ToolMessage("line\n" * 5_000, tool_call_id="g"),
    ]

    assert bounded_history(_history(5) + current, 500) == current


def test_without_any_user_message_everything_is_sent() -> None:
    only_ai: list[AnyMessage] = [AIMessage("a"), AIMessage("b")]
    assert bounded_history(only_ai, 10) == only_ai


def test_agent_node_sends_only_the_bounded_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[list[Any]] = []

    def _capture(messages: list[Any]) -> AIMessage:
        sent.append(messages)
        return AIMessage("ok")

    fake = MagicMock()
    fake.invoke.side_effect = _capture
    monkeypatch.setattr(nodes, "llm_with_tools", fake)
    monkeypatch.setattr(settings, "MODEL_HISTORY_TOKEN_BUDGET", 2_000)
    history = _history(200)

    nodes.agent_node(
        {
            "messages": history,
            "ledger_id": "1",
            "message_time": _MESSAGE_TIME,
            "trip_start_date": None,
            "trip_timezone": "Asia/Singapore",
            "local_date": "2026-10-09",
        }
    )

    system, *rest = sent[0]
    assert isinstance(system, SystemMessage)
    assert rest == bounded_history(history, 2_000)
    assert len(rest) < len(history)


# ── saving once per turn, on the real checkpointer ───────────────────────────


class _ScriptedModel:
    def __init__(self, replies: list[AIMessage]) -> None:
        self.replies = list(replies)

    def invoke(self, _messages: list[Any]) -> AIMessage:
        return self.replies.pop(0)


def _thread_items(client: DynamoDBClient, thread_id: str) -> int:
    """Every checkpointer item for a thread: snapshots, their chunks, pending writes."""
    items = client.scan(TableName=settings.DYNAMODB_TABLE_NAME)["Items"]
    prefixes = tuple(
        f"{kind}_{thread_id}" for kind in ("CHECKPOINT", "CHUNK", "WRITES")
    )
    return sum(1 for i in items if i["PK"]["S"].startswith(prefixes))


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _turn_with_a_tool_call(thread_id: str, durability: Any) -> None:
    model = _ScriptedModel(
        [
            AIMessage(
                "", tool_calls=[{"id": "g", "name": "get_all_expenses", "args": {}}]
            ),
            AIMessage("You have none."),
        ]
    )
    with patch.object(nodes, "llm_with_tools", model):
        graph_module.build_graph().invoke(
            {
                "messages": [HumanMessage("show all")],
                "ledger_id": "1",
                "message_time": _MESSAGE_TIME,
            },
            _config(thread_id),
            durability=durability,
        )


def test_saving_once_per_turn_writes_a_fraction_of_the_items(
    dynamodb_table: DynamoDBClient,
) -> None:
    _turn_with_a_tool_call("per-step", "sync")
    _turn_with_a_tool_call("per-turn", CHECKPOINT_DURABILITY)

    per_step = _thread_items(dynamodb_table, "per-step")
    per_turn = _thread_items(dynamodb_table, "per-turn")
    assert per_turn <= 2
    assert per_step > 5 * per_turn


def test_a_turn_saved_once_resumes_with_its_whole_conversation(
    dynamodb_table: DynamoDBClient,
) -> None:
    _turn_with_a_tool_call("t1", CHECKPOINT_DURABILITY)

    state = graph_module.build_graph().get_state(_config("t1"))

    assert [type(m).__name__ for m in state.values["messages"]] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]


def test_a_paused_trip_end_resumes_when_saved_once_per_turn(
    dynamodb_table: DynamoDBClient,
) -> None:
    # The run stops at the end-trip confirmation; that pause is when the one snapshot is
    # written, and the resume must run end_trip from it.
    dynamodb.put_item({"PK": "USER#1", "SK": "TRIP#ACTIVE", "start_date": "2026-10-09"})
    app = graph_module.build_graph()
    model = _ScriptedModel(
        [
            AIMessage("", tool_calls=[{"id": "e", "name": "end_trip", "args": {}}]),
            AIMessage("Trip summary!"),
        ]
    )
    with (
        patch.object(nodes, "llm_with_tools", model),
        patch("src.bot.tools.trip.get_sgd_exchange_rates", return_value={}),
    ):
        app.invoke(
            {
                "messages": [HumanMessage("end trip")],
                "ledger_id": "1",
                "message_time": _MESSAGE_TIME,
            },
            _config("t2"),
            durability=CHECKPOINT_DURABILITY,
        )
        assert app.get_state(_config("t2")).next == (END_TRIP_NODE,)

        result = app.invoke(None, _config("t2"), durability=CHECKPOINT_DURABILITY)

    assert result["messages"][-1].content == "Trip summary!"
    assert dynamodb.get_item("USER#1", "TRIP#ACTIVE") is None
