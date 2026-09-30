"""Tests for delivering replies: splitting, the plain-text fallback, and callback edits.

`_parse_mode` sends anything containing "<" as HTML, so a reply whose markup does not
parse — an unclosed tag, "cost < 5", or a tag pair cut in two by the splitter — was
rejected outright and lost. These tests pin the fallback that delivers it unformatted.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from telegram import Message
from telegram.error import BadRequest

from src.bot.telegram_handler import (
    _MAX_MESSAGE_LENGTH,
    _edit_in_chunks,
    _reply_in_chunks,
    _send_formatted,
)

_PARSE_ERROR = BadRequest(
    "Can't parse entities: can't find end tag corresponding to start tag \"b\""
)


def _rejects_html(error: Exception = _PARSE_ERROR) -> AsyncMock:
    """A send method that fails on HTML and accepts plain text, as Telegram does."""

    async def send(text: str, parse_mode: str | None = None) -> None:
        if parse_mode is not None:
            raise error

    return AsyncMock(side_effect=send)


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


# ── _send_formatted ──────────────────────────────────────────────────────────


async def test_plain_text_is_sent_without_a_parse_mode() -> None:
    send = AsyncMock()

    await _send_formatted(send, "Lunch recorded.")

    send.assert_awaited_once_with("Lunch recorded.", parse_mode=None)


async def test_markup_is_sent_as_html() -> None:
    send = AsyncMock()

    await _send_formatted(send, "<b>Total</b>: SGD 12")

    send.assert_awaited_once_with("<b>Total</b>: SGD 12", parse_mode="HTML")


async def test_unparseable_html_is_resent_as_plain_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The failure this exists for: without it the reply is lost and the user sees only
    # the generic error message.
    send = _rejects_html()

    with caplog.at_level(logging.WARNING):
        await _send_formatted(send, "<b>Total: SGD 12")

    assert send.await_args_list == [
        call("<b>Total: SGD 12", parse_mode="HTML"),
        call("<b>Total: SGD 12", parse_mode=None),
    ]
    assert "resending as plain text" in caplog.text


async def test_a_literal_less_than_sign_is_still_delivered() -> None:
    # Not a split at all: any "<" selects HTML, so an ordinary comparison was enough.
    send = _rejects_html(
        BadRequest('Can\'t parse entities: unsupported start tag " 5"')
    )

    await _send_formatted(send, "Only items that cost < 5 SGD")

    assert send.await_args_list[-1] == call(
        "Only items that cost < 5 SGD", parse_mode=None
    )


async def test_other_bad_requests_are_not_retried() -> None:
    send = AsyncMock(side_effect=BadRequest("Chat not found"))

    with pytest.raises(BadRequest, match="Chat not found"):
        await _send_formatted(send, "<b>Total</b>")

    send.assert_awaited_once()


async def test_plain_text_is_not_retried_when_rejected() -> None:
    # A parse error is impossible without a parse mode; if one appears anyway, resending
    # the identical request would only fail the same way.
    send = AsyncMock(side_effect=_PARSE_ERROR)

    with pytest.raises(BadRequest):
        await _send_formatted(send, "no markup here")

    send.assert_awaited_once()


async def test_a_failed_plain_text_resend_propagates() -> None:
    send = AsyncMock(side_effect=[_PARSE_ERROR, BadRequest("Chat not found")])

    with pytest.raises(BadRequest, match="Chat not found"):
        await _send_formatted(send, "<b>Total")


# ── _reply_in_chunks ─────────────────────────────────────────────────────────


async def test_short_reply_is_one_message() -> None:
    message = _message()

    await _reply_in_chunks(message, "Lunch recorded.")

    message.reply_text.assert_awaited_once_with("Lunch recorded.", parse_mode=None)


async def test_long_reply_is_sent_in_order_within_the_limit() -> None:
    message = _message()
    content = _long_content(100)

    await _reply_in_chunks(message, content)

    sent = [c.args[0] for c in message.reply_text.await_args_list]
    assert len(sent) > 1
    assert all(len(chunk) <= _MAX_MESSAGE_LENGTH for chunk in sent)
    assert "\n".join(sent) == content


# ── _edit_in_chunks ──────────────────────────────────────────────────────────


async def test_short_content_only_edits_the_message() -> None:
    message = _message()
    query = _query(message)

    await _edit_in_chunks(query, "Trip ending cancelled.")

    query.edit_message_text.assert_awaited_once_with(
        "Trip ending cancelled.", parse_mode=None
    )
    message.reply_text.assert_not_awaited()


async def test_overflow_is_sent_as_replies_after_the_edit() -> None:
    message = _message()
    query = _query(message)
    content = _long_content(100)

    await _edit_in_chunks(query, content)

    edited = query.edit_message_text.await_args.args[0]
    replies = [c.args[0] for c in message.reply_text.await_args_list]
    assert replies
    assert "\n".join([edited, *replies]) == content


async def test_overflow_chunk_with_broken_html_falls_back() -> None:
    # A tag pair cut in two by the splitter leaves the second chunk starting with an
    # unmatched closing tag. The first chunk has already been delivered by then, so a
    # raise here would strand the summary half-sent.
    message = _message()
    message.reply_text = _rejects_html()
    query = _query(message)
    content = "x" * (_MAX_MESSAGE_LENGTH - 5) + "\n<b>SGD 12</b>\nend</b>"

    await _edit_in_chunks(query, content)

    assert message.reply_text.await_args_list[-1].kwargs == {"parse_mode": None}


async def test_overflow_is_dropped_with_a_warning_when_the_message_is_inaccessible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = _query(message=None)

    with caplog.at_level(logging.WARNING):
        await _edit_in_chunks(query, _long_content(100))

    query.edit_message_text.assert_awaited_once()
    assert "overflow chunk(s) were not sent" in caplog.text
