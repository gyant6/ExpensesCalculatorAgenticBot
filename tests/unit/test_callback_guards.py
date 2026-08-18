"""Tests for the inline-keyboard callback guards.

Both guards exist because Telegram rejects a call that a repeated tap makes redundant,
and both were found by a real double tap rather than by reasoning: the confirmation claim
after a duplicate resumed `end_trip` twice, and the acknowledgement after a tap queued
behind a slow trip end reached Telegram with an ID it had already discarded.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from src.bot.telegram_handler import (
    _ENDING_TRIP_NOTICE,
    _acknowledge_callback,
    _claim_confirmation,
)


def _query(
    answer_error: Exception | None = None, edit_error: Exception | None = None
) -> MagicMock:
    query = MagicMock()
    query.answer = AsyncMock(side_effect=answer_error)
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock(side_effect=edit_error)
    return query


class _FakeMessage:
    """A message whose edits behave as Telegram's do, for the double-tap sequence.

    Rejects an edit that would change nothing — which is what makes the keyboard removal
    usable as a lock, and what makes a text write unusable as one.
    """

    def __init__(self) -> None:
        self.text = "Are you sure you want to end the trip?"
        self.has_keyboard = True

    async def edit_message_reply_markup(self, reply_markup: object = None) -> None:
        if not self.has_keyboard:
            raise BadRequest("Message is not modified")
        self.has_keyboard = False

    async def edit_message_text(self, text: str, **_kwargs: object) -> None:
        if text == self.text:
            raise BadRequest("Message is not modified")
        self.text = text


# ── _acknowledge_callback ────────────────────────────────────────────────────


async def test_acknowledges_a_live_query() -> None:
    query = _query()

    await _acknowledge_callback(query)

    query.answer.assert_awaited_once()


async def test_expired_query_is_swallowed_as_a_repeated_tap() -> None:
    # Updates are processed one at a time, so a second tap queued behind a trip end
    # arrives after Telegram has discarded the query. Raising here would abort the
    # handler before the confirmation claim, leaving the duplicate to be stopped by a
    # crash rather than by the guard.
    query = _query(
        answer_error=BadRequest(
            "Query is too old and response timeout expired or query id is invalid"
        )
    )

    await _acknowledge_callback(query)


async def test_other_bad_requests_still_propagate() -> None:
    query = _query(answer_error=BadRequest("Chat not found"))

    with pytest.raises(BadRequest, match="Chat not found"):
        await _acknowledge_callback(query)


# ── _claim_confirmation ──────────────────────────────────────────────────────


async def test_claim_removes_the_keyboard() -> None:
    query = _query()

    assert await _claim_confirmation(query) is True

    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


async def test_second_tap_finds_the_keyboard_already_gone() -> None:
    # This is the lock: the API rejects an edit that changes nothing, so of two taps
    # exactly one removes the keyboard and owns the confirmation.
    query = _query(edit_error=BadRequest("Message is not modified"))

    assert await _claim_confirmation(query) is False


async def test_other_bad_requests_are_not_mistaken_for_a_lost_claim() -> None:
    query = _query(edit_error=BadRequest("Message to edit not found"))

    with pytest.raises(BadRequest, match="not found"):
        await _claim_confirmation(query)


async def test_claim_still_fails_after_the_winner_rewrote_the_message() -> None:
    # The regression this guards against. Updates are processed one at a time, so a
    # second tap runs only after the first has finished and replaced the message text
    # with the trip summary. A claim keyed on writing known text would succeed here —
    # the summary differs from that text — and go on to overwrite the summary. Keying it
    # on the keyboard removal holds, because the keyboard is gone for good.
    message = _FakeMessage()
    first, second = MagicMock(), MagicMock()
    for query in (first, second):
        query.edit_message_reply_markup = message.edit_message_reply_markup
        query.edit_message_text = message.edit_message_text

    assert await _claim_confirmation(first) is True
    await first.edit_message_text(_ENDING_TRIP_NOTICE)
    await first.edit_message_text("Trip ended. You spent SGD 7.00.")

    assert await _claim_confirmation(second) is False
    assert message.text == "Trip ended. You spent SGD 7.00."
