"""Tests for delivering replies: splitting, plain text, and callback edits.

Replies are sent without a parse mode. They used to go out as HTML whenever they contained
"<", and since the prompt asks the model for plain text, that "<" was always literal —
"expenses < 10 SGD" was rejected by Telegram and the reply lost. Every send here is
asserted to carry no parse mode, so that cannot return unnoticed.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Message

from src.bot.telegram_handler import (
    _MAX_MESSAGE_LENGTH,
    _edit_in_chunks,
    _reply_in_chunks,
)


def _message() -> MagicMock:
    message = MagicMock(spec=Message)
    message.reply_text = AsyncMock()
    return message


def _query(message: object) -> MagicMock:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message = message
    return query


def _long_content(lines: int) -> str:
    # Each line is 100 characters plus its newline, so 50 lines overflow one message.
    return "\n".join(f"{i:03d}" + "x" * 97 for i in range(lines))


def _sent_texts(send: AsyncMock) -> list[str]:
    """Texts passed to a send method, asserting none was given formatting options."""
    for awaited in send.await_args_list:
        assert awaited.kwargs == {}, f"reply sent with options {awaited.kwargs}"
    return [awaited.args[0] for awaited in send.await_args_list]


# ── _reply_in_chunks ─────────────────────────────────────────────────────────


async def test_short_reply_is_one_plain_message() -> None:
    message = _message()

    await _reply_in_chunks(message, "Lunch recorded.")

    assert _sent_texts(message.reply_text) == ["Lunch recorded."]


async def test_a_literal_less_than_sign_is_sent_as_plain_text() -> None:
    # The production failure: with HTML selected, Telegram rejected this as an
    # unsupported start tag.
    message = _message()

    await _reply_in_chunks(message, "Here are your expenses < 10 SGD")

    assert _sent_texts(message.reply_text) == ["Here are your expenses < 10 SGD"]


async def test_long_reply_is_sent_in_order_within_the_limit() -> None:
    message = _message()
    content = _long_content(100)

    await _reply_in_chunks(message, content)

    sent = _sent_texts(message.reply_text)
    assert len(sent) > 1
    assert all(len(chunk) <= _MAX_MESSAGE_LENGTH for chunk in sent)
    assert "\n".join(sent) == content


# ── _edit_in_chunks ──────────────────────────────────────────────────────────


async def test_short_content_only_edits_the_message() -> None:
    message = _message()
    query = _query(message)

    await _edit_in_chunks(query, "Trip ending cancelled.")

    assert _sent_texts(query.edit_message_text) == ["Trip ending cancelled."]
    message.reply_text.assert_not_awaited()


async def test_overflow_is_sent_as_plain_replies_after_the_edit() -> None:
    message = _message()
    query = _query(message)
    content = _long_content(100) + "\nTotal < 500 SGD"

    await _edit_in_chunks(query, content)

    edited = _sent_texts(query.edit_message_text)
    replies = _sent_texts(message.reply_text)
    assert replies
    assert "\n".join([*edited, *replies]) == content


async def test_overflow_is_dropped_with_a_warning_when_the_message_is_inaccessible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = _query(message=None)

    with caplog.at_level(logging.WARNING):
        await _edit_in_chunks(query, _long_content(100))

    query.edit_message_text.assert_awaited_once()
    assert "overflow chunk(s) were not sent" in caplog.text
