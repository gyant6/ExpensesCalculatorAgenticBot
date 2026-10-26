"""Tests for confirming a trip end, and for the versioned conversation thread.

On 9 Oct 2026 the resume after the end-trip confirmation returned without running
end_trip — the thread had been written under an older state shape — and the handler sent
"Trip ended." and cleared the history while the trip and its expenses were untouched.
The handler must now check the trip really ended, and threads carry a schema version so
a state change never resumes an old one.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from telegram import Message

from src.bot import telegram_handler
from src.bot.agent.graph import (
    CHECKPOINT_DURABILITY,
    END_TRIP_NODE,
    THREAD_SCHEMA_VERSION,
    thread_id_for,
)
from src.bot.conversation import ConfirmationResult, render_confirmed_trip
from src.bot.export import generate_csv
from src.bot.tools.trip import END_TRIP_SUCCESS

LEDGER = 111111111


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A confirmed end-trip tap with the graph, storage and Telegram replaced."""
    graph = MagicMock()
    graph.get_state.return_value = MagicMock(next=(END_TRIP_NODE,))
    csv_bytes = generate_csv(
        [
            {
                "date": "2026-10-09",
                "summary": "Lunch",
                "category": "Food",
                "amount": "20",
                "currency": "SGD",
                "payment_method": "Cash",
            }
        ],
        {},
    )
    graph.invoke.return_value = {
        "messages": [
            ToolMessage(
                f"{END_TRIP_SUCCESS}\n\n{csv_bytes.decode()}",
                tool_call_id="t",
                name="end_trip",
            ),
            AIMessage("Trip summary: SGD 200.00"),
        ]
    }
    monkeypatch.setattr(telegram_handler, "_graph", graph)

    monkeypatch.setattr(telegram_handler, "_acknowledge_callback", AsyncMock())
    monkeypatch.setattr(
        telegram_handler, "_claim_confirmation", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(telegram_handler, "list_expenses", MagicMock(return_value=[]))
    monkeypatch.setattr(
        telegram_handler,
        "_render_attachments",
        MagicMock(return_value=(b"pie", b"bar", b"different CSV")),
    )
    clear = MagicMock()
    monkeypatch.setattr(telegram_handler, "clear_thread_history", clear)
    trip_item = MagicMock(return_value=None)
    monkeypatch.setattr(telegram_handler, "get_item", trip_item)

    message = MagicMock(spec=Message)
    message.reply_text = AsyncMock()
    message.reply_photo = AsyncMock()
    message.reply_document = AsyncMock()
    query = MagicMock()
    query.data = telegram_handler._END_TRIP_CONFIRM
    query.edit_message_text = AsyncMock()
    query.message = message

    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = LEDGER
    return {
        "update": update,
        "query": query,
        "message": message,
        "graph": graph,
        "clear": clear,
        "trip_item": trip_item,
        "csv_bytes": csv_bytes,
    }


async def test_a_resume_that_did_not_end_the_trip_is_reported_as_a_failure(
    harness: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    # The production failure: the resume returns the pending end_trip call unexecuted.
    harness["graph"].invoke.return_value = {
        "messages": [
            AIMessage("", tool_calls=[{"id": "b", "name": "end_trip", "args": {}}])
        ]
    }
    harness["trip_item"].return_value = {"SK": "TRIP#ACTIVE"}

    with caplog.at_level(logging.ERROR):
        await telegram_handler.handle_callback(harness["update"], MagicMock())

    last_edit = harness["query"].edit_message_text.await_args
    assert last_edit.args[0] == telegram_handler._END_TRIP_FAILED
    harness["message"].reply_photo.assert_not_awaited()
    harness["message"].reply_document.assert_not_awaited()
    assert "end_trip did not run" in caplog.text
    # Cleared so the thread is not left paused at a confirmation with no buttons.
    harness["clear"].assert_called_once_with(
        harness["graph"], thread_id_for(str(LEDGER))
    )


async def test_an_ended_trip_sends_the_summary_and_files_then_clears(
    harness: dict[str, Any],
) -> None:
    await telegram_handler.handle_callback(harness["update"], MagicMock())

    last_edit = harness["query"].edit_message_text.await_args
    assert (
        last_edit.args[0]
        == "Trip ended! 1 expense recorded. 🐾\nTotal: SGD 20.00\nFood: SGD 20.00"
    )
    assert harness["message"].reply_photo.await_count == 2
    assert harness["trip_item"].call_args.kwargs == {"consistent_read": True}
    harness["message"].reply_document.assert_awaited_once()
    assert (
        harness["message"].reply_document.await_args.args[0].getvalue()
        == harness["csv_bytes"]
    )
    harness["clear"].assert_called_once_with(
        harness["graph"], thread_id_for(str(LEDGER))
    )


async def test_the_graph_is_resumed_on_the_versioned_thread_saving_once(
    harness: dict[str, Any],
) -> None:
    await telegram_handler.handle_callback(harness["update"], MagicMock())

    resume = harness["graph"].invoke.call_args
    assert resume.args[1] == {"configurable": {"thread_id": thread_id_for(str(LEDGER))}}
    assert resume.kwargs == {"durability": CHECKPOINT_DURABILITY}


def test_thread_ids_carry_the_schema_version() -> None:
    assert thread_id_for("123") == f"123:v{THREAD_SCHEMA_VERSION}"
    assert THREAD_SCHEMA_VERSION >= 2


def test_confirmed_trip_uses_valid_fallback_csv_after_damaged_tool_output() -> None:
    csv_bytes = generate_csv(
        [
            {
                "date": "2026-10-09",
                "summary": "Lunch",
                "category": "Food",
                "amount": "20",
                "currency": "SGD",
                "payment_method": "Cash",
            }
        ],
        {},
    )
    confirmation = ConfirmationResult(
        result={"messages": [AIMessage("SGD 200.00")]},
        trip_ended=True,
        csv_bytes=b"damaged",
    )

    content, delivered_csv = render_confirmed_trip(confirmation, csv_bytes)

    assert "Total: SGD 20.00" in content
    assert delivered_csv == csv_bytes
