"""Tests for the diagnostic logged when a turn produces no text.

Seen once in production: the tool ran and the expense was stored, but the final message
carried nothing to send. Whether the model returned no text or `_extract_text` discarded
an unrecognised block shape is indistinguishable from the outside, so the fallback logs
what it saw rather than silently substituting text.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from src.bot.telegram_handler import _extract_text, _log_empty_reply


def test_blocks_without_a_text_type_extract_to_nothing() -> None:
    # The failure mode the logging exists to expose: a shape _extract_text does not
    # recognise is dropped in full, and the turn looks identical to the model going quiet.
    unrecognised: list[Any] = [{"kind": "text", "text": "this shape is not matched"}]

    assert _extract_text(unrecognised) == ""


def test_logs_block_shape_not_content(caplog: pytest.LogCaptureFixture) -> None:
    message = AIMessage(content=[{"type": "text", "text": "1200 yen at Ichiran ramen"}])

    with caplog.at_level(logging.WARNING):
        _log_empty_reply("35153600", message)

    record = caplog.text
    assert "AIMessage" in record
    assert "35153600" in record
    # The user's expense text must not be copied into logs.
    assert "Ichiran" not in record


def test_handles_a_plain_string_content(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _log_empty_reply("111", AIMessage(content=""))

    assert "blocks=str" in caplog.text


def test_reports_tool_call_count(caplog: pytest.LogCaptureFixture) -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"id": "1", "name": "add_expense", "args": {}}],
    )

    with caplog.at_level(logging.WARNING):
        _log_empty_reply("111", message)

    assert "tool_calls=1" in caplog.text
