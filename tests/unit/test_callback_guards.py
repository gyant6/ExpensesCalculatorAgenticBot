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
    query.edit_message_text = AsyncMock(side_effect=edit_error)
    return query


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


async def test_claim_replaces_the_keyboard_with_the_progress_notice() -> None:
    query = _query()

    assert await _claim_confirmation(query) is True

    query.edit_message_text.assert_awaited_once_with(
        _ENDING_TRIP_NOTICE, reply_markup=None
    )


async def test_second_tap_writing_identical_text_loses_the_claim() -> None:
    # This is the lock: the API rejects an edit that changes nothing, so of two taps
    # exactly one rewrites the message and owns the confirmation.
    query = _query(
        edit_error=BadRequest("Message is not modified: specified new message content")
    )

    assert await _claim_confirmation(query) is False


async def test_other_bad_requests_are_not_mistaken_for_a_lost_claim() -> None:
    query = _query(edit_error=BadRequest("Message to edit not found"))

    with pytest.raises(BadRequest, match="not found"):
        await _claim_confirmation(query)
