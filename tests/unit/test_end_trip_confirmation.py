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
from langchain_core.messages import AIMessage
from telegram import Message

from src.bot import telegram_handler
from src.bot.agent.graph import END_TRIP_NODE, THREAD_SCHEMA_VERSION, thread_id_for

LEDGER = 111111111


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A confirmed end-trip tap with the graph, storage and Telegram replaced."""
    graph = MagicMock()
    graph.get_state.return_value = MagicMock(next=(END_TRIP_NODE,))
    graph.invoke.return_value = {"messages": [AIMessage("Trip summary: SGD 20.00")]}
    monkeypatch.setattr(telegram_handler, "_graph", graph)

    monkeypatch.setattr(telegram_handler, "_acknowledge_callback", AsyncMock())
    monkeypatch.setattr(
        telegram_handler, "_claim_confirmation", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(telegram_handler, "list_expenses", MagicMock(return_value=[]))
    monkeypatch.setattr(
        telegram_handler,
        "_render_attachments",
        MagicMock(return_value=(b"pie", b"bar", b"csv")),
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
    assert last_edit.args[0] == "Trip summary: SGD 20.00"
    assert harness["message"].reply_photo.await_count == 2
    harness["message"].reply_document.assert_awaited_once()
    harness["clear"].assert_called_once_with(
        harness["graph"], thread_id_for(str(LEDGER))
    )


async def test_the_graph_is_resumed_on_the_versioned_thread(
    harness: dict[str, Any],
) -> None:
    await telegram_handler.handle_callback(harness["update"], MagicMock())

    config = harness["graph"].invoke.call_args.args[1]
    assert config == {"configurable": {"thread_id": thread_id_for(str(LEDGER))}}


def test_thread_ids_carry_the_schema_version() -> None:
    assert thread_id_for("123") == f"123:v{THREAD_SCHEMA_VERSION}"
    assert THREAD_SCHEMA_VERSION >= 2
