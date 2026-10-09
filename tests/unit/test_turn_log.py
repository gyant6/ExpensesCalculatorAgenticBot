"""Tests for the per-turn log record, and for the handlers writing one.

The record is the only account of a conversation once its trip ends, so it must hold
what the user said, each tool call with what it returned, and the reply — and only this
turn's messages, not the history the model also saw. A turn that fails must still be
recorded, since failures are what it is read for.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from src.bot import telegram_handler
from src.bot.agent.graph import END_TRIP_NODE
from src.bot.turn_log import (
    TEXT_MAX_CHARS,
    TOOL_RESULT_MAX_CHARS,
    TurnKind,
    TurnOutcome,
    log_turn,
)

CHAT = "111111111"
SENDER = 222222222


def _usage(input_tokens: int, output_tokens: int, cache_read: int = 0) -> Any:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_token_details": {"cache_read": cache_read},
    }


def _turn_messages() -> list[BaseMessage]:
    return [
        HumanMessage("taxi 8500 won"),
        AIMessage(
            "",
            tool_calls=[
                {"id": "c1", "name": "add_expense", "args": {"amount": "8500"}}
            ],
            usage_metadata=_usage(4100, 120, cache_read=4000),
        ),
        ToolMessage("Expense recorded with id p9x4.", tool_call_id="c1"),
        AIMessage("Logged! 🚕", usage_metadata=_usage(4300, 40)),
    ]


def _fields(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    records = [
        r for r in caplog.records if getattr(r, "fields", {}).get("event") == "turn"
    ]
    assert len(records) == 1
    fields: dict[str, Any] = records[0].fields  # type: ignore[attr-defined]
    return fields


def _log(**overrides: Any) -> None:
    arguments: dict[str, Any] = {
        "kind": TurnKind.MESSAGE,
        "outcome": TurnOutcome.REPLIED,
        "chat": CHAT,
        "sender": SENDER,
        "update_id": 7,
        "new_messages": _turn_messages(),
        "reply": "Logged! 🚕",
        "timings": {"graph": 3100},
    }
    log_turn(**{**arguments, **overrides})


# ── the record ───────────────────────────────────────────────────────────────────────


def test_a_turn_records_message_steps_reply_and_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        _log()

    fields = _fields(caplog)
    assert fields["kind"] == "message"
    assert fields["outcome"] == "replied"
    assert (fields["chat"], fields["sender"], fields["update_id"]) == (CHAT, SENDER, 7)
    assert fields["user_message"] == "taxi 8500 won"
    assert fields["steps"] == [
        {
            "tool": "add_expense",
            "args": {"amount": "8500"},
            "result": "Expense recorded with id p9x4.",
        }
    ]
    assert fields["reply"] == "Logged! 🚕"
    assert fields["model_calls"] == 2
    assert (fields["input_tokens"], fields["output_tokens"]) == (8400, 160)
    assert fields["cache_read_tokens"] == 4000
    assert fields["timings_ms"] == {"graph": 3100}
    assert fields["error"] is None
    assert caplog.records[-1].levelno == logging.INFO


def test_a_long_tool_result_is_cut_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    listing = "x" * (TOOL_RESULT_MAX_CHARS + 25)
    messages = [
        AIMessage("", tool_calls=[{"id": "l", "name": "get_all_expenses", "args": {}}]),
        ToolMessage(listing, tool_call_id="l"),
    ]
    with caplog.at_level(logging.INFO):
        _log(new_messages=messages, reply="x" * (TEXT_MAX_CHARS + 1))

    fields = _fields(caplog)
    result = fields["steps"][0]["result"]
    assert result.startswith("x" * TOOL_RESULT_MAX_CHARS)
    assert result.endswith("[25 more characters]")
    assert fields["reply"].endswith("[1 more characters]")


def test_a_refused_tool_call_records_its_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = [
        AIMessage("", tool_calls=[{"id": "e", "name": "edit_expense", "args": {}}]),
        ToolMessage("no such expense", tool_call_id="e", status="error"),
    ]
    with caplog.at_level(logging.INFO):
        _log(new_messages=messages)

    assert _fields(caplog)["steps"][0]["status"] == "error"


def test_a_result_for_an_earlier_turns_call_is_listed_on_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # After "Yes": end_trip was called in the previous turn; only its result is new.
    messages = [
        ToolMessage("date,summary,amount\n...", tool_call_id="t", name="end_trip"),
        AIMessage("Trip summary"),
    ]
    with caplog.at_level(logging.INFO):
        _log(kind=TurnKind.END_TRIP_CONFIRMED, new_messages=messages)

    assert _fields(caplog)["steps"] == [
        {"tool": "end_trip", "result": "date,summary,amount\n..."}
    ]


def test_a_call_awaiting_confirmation_is_listed_without_a_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = [
        HumanMessage("end trip"),
        AIMessage("", tool_calls=[{"id": "t", "name": "end_trip", "args": {}}]),
    ]
    with caplog.at_level(logging.INFO):
        _log(new_messages=messages, outcome=TurnOutcome.CONFIRMATION_ASKED)

    assert _fields(caplog)["steps"] == [{"tool": "end_trip", "args": {}}]


def test_a_failed_turn_is_a_warning_with_the_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        _log(
            outcome=TurnOutcome.ERROR,
            new_messages=[HumanMessage("lunch 12")],
            reply=None,
            error=TimeoutError("Bedrock timed out"),
        )

    fields = _fields(caplog)
    assert fields["error"] == "TimeoutError: Bedrock timed out"
    assert fields["user_message"] == "lunch 12"
    assert caplog.records[-1].levelno == logging.WARNING


def test_counts_are_recorded(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        _log(kind=TurnKind.END_TRIP_CONFIRMED, expenses=12)

    assert _fields(caplog)["expenses"] == 12


# ── the message handler writes one ───────────────────────────────────────────────────

_HISTORY: list[BaseMessage] = [HumanMessage("hi"), AIMessage("Woof!")]


@pytest.fixture
def message_turn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An authorised private message with the graph and Telegram replaced."""
    graph = MagicMock()
    graph.get_state.return_value = MagicMock(values={"messages": _HISTORY}, next=())
    graph.invoke.return_value = {"messages": [*_HISTORY, *_turn_messages()]}
    monkeypatch.setattr(telegram_handler, "_graph", graph)
    monkeypatch.setattr(
        telegram_handler, "_addressed_to_someone_else", MagicMock(return_value=False)
    )
    monkeypatch.setattr(telegram_handler, "_check_auth", AsyncMock(return_value=True))
    reply = AsyncMock()
    monkeypatch.setattr(telegram_handler, "_reply_in_chunks", reply)

    update = MagicMock()
    update.update_id = 7
    update.message.text = "taxi 8500 won"
    update.message.date = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)
    update.message.reply_text = AsyncMock()
    update.effective_user.id = int(CHAT)
    update.effective_chat.type = "private"
    return {"update": update, "graph": graph, "reply": reply}


async def test_a_message_turn_logs_only_its_own_messages(
    message_turn: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await telegram_handler.handle_message(message_turn["update"], MagicMock())

    fields = _fields(caplog)
    assert fields["user_message"] == "taxi 8500 won"
    assert [s["tool"] for s in fields["steps"]] == ["add_expense"]
    assert fields["model_calls"] == 2
    assert fields["reply"] == "Logged! 🚕"
    assert fields["update_id"] == 7
    assert set(fields["timings_ms"]) >= {"auth", "state", "graph", "reply"}


async def test_a_message_turn_that_asks_to_confirm_records_the_prompt(
    message_turn: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    graph = message_turn["graph"]
    graph.get_state.side_effect = [
        MagicMock(values={"messages": _HISTORY}, next=()),
        MagicMock(next=(END_TRIP_NODE,)),
    ]
    with caplog.at_level(logging.INFO):
        await telegram_handler.handle_message(message_turn["update"], MagicMock())

    fields = _fields(caplog)
    assert fields["outcome"] == "confirmation_asked"
    assert fields["reply"] == telegram_handler._END_TRIP_PROMPT


async def test_a_message_turn_that_fails_is_logged_then_raised(
    message_turn: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    message_turn["graph"].invoke.side_effect = TimeoutError("Bedrock timed out")

    with caplog.at_level(logging.INFO), pytest.raises(TimeoutError):
        await telegram_handler.handle_message(message_turn["update"], MagicMock())

    fields = _fields(caplog)
    assert fields["outcome"] == "error"
    assert fields["user_message"] == "taxi 8500 won"
    assert fields["error"] == "TimeoutError: Bedrock timed out"
    message_turn["reply"].assert_not_awaited()


# ── the end-trip buttons write one ───────────────────────────────────────────────────

_PAUSED: list[BaseMessage] = [
    HumanMessage("end trip"),
    AIMessage("", tool_calls=[{"id": "t", "name": "end_trip", "args": {}}]),
]


@pytest.fixture
def button_turn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A tap on the end-trip keyboard, with the graph, storage and Telegram replaced."""
    graph = MagicMock()
    graph.get_state.return_value = MagicMock(
        values={"messages": _PAUSED}, next=(END_TRIP_NODE,)
    )
    monkeypatch.setattr(telegram_handler, "_graph", graph)
    monkeypatch.setattr(telegram_handler, "_acknowledge_callback", AsyncMock())
    monkeypatch.setattr(
        telegram_handler, "_claim_confirmation", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        telegram_handler, "list_expenses", MagicMock(return_value=[{}, {}, {}])
    )
    monkeypatch.setattr(
        telegram_handler,
        "_render_attachments",
        MagicMock(return_value=(b"pie", b"bar", b"csv")),
    )
    monkeypatch.setattr(telegram_handler, "clear_thread_history", MagicMock())
    monkeypatch.setattr(telegram_handler, "get_item", MagicMock(return_value=None))
    monkeypatch.setattr(telegram_handler, "_edit_in_chunks", AsyncMock())
    monkeypatch.setattr(telegram_handler, "_send_attachments", AsyncMock())

    update = MagicMock()
    update.update_id = 8
    update.effective_user.id = SENDER
    update.effective_chat.id = int(CHAT)
    update.callback_query.edit_message_text = AsyncMock()
    return {"update": update, "graph": graph}


async def test_a_confirmed_end_logs_end_trips_result_and_the_expense_count(
    button_turn: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    update, graph = button_turn["update"], button_turn["graph"]
    update.callback_query.data = telegram_handler._END_TRIP_CONFIRM
    graph.invoke.return_value = {
        "messages": [
            *_PAUSED,
            ToolMessage("date,amount\n2026-10-09,5", tool_call_id="t", name="end_trip"),
            AIMessage("Trip summary: SGD 5.00"),
        ]
    }

    with caplog.at_level(logging.INFO):
        await telegram_handler.handle_callback(update, MagicMock())

    fields = _fields(caplog)
    assert fields["kind"] == "end_trip_confirmed"
    assert fields["outcome"] == "replied"
    assert fields["user_message"] is None
    assert fields["steps"] == [
        {"tool": "end_trip", "result": "date,amount\n2026-10-09,5"}
    ]
    assert fields["reply"] == "Trip summary: SGD 5.00"
    assert fields["expenses"] == 3
    assert (fields["sender"], fields["update_id"]) == (SENDER, 8)


async def test_a_cancelled_end_logs_the_cancellation_against_end_trip(
    button_turn: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    update, graph = button_turn["update"], button_turn["graph"]
    update.callback_query.data = telegram_handler._END_TRIP_CANCEL
    graph.invoke.return_value = {
        "messages": [
            *_PAUSED,
            ToolMessage(
                "User cancelled ending the trip.", tool_call_id="t", name="end_trip"
            ),
            AIMessage("Trip end cancelled!"),
        ]
    }

    with caplog.at_level(logging.INFO):
        await telegram_handler.handle_callback(update, MagicMock())

    injected = graph.update_state.call_args.args[1]["messages"][0]
    assert (injected.tool_call_id, injected.name) == ("t", "end_trip")
    fields = _fields(caplog)
    assert fields["kind"] == "end_trip_cancelled"
    assert fields["steps"] == [
        {"tool": "end_trip", "result": "User cancelled ending the trip."}
    ]
    assert fields["reply"] == "Trip end cancelled!"
