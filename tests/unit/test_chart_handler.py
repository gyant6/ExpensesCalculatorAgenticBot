"""Tests for the chart Lambda entrypoint."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from src.bot.chart_handler import lambda_handler
from src.bot.chart_protocol import (
    REQUEST_EXPENSES,
    REQUEST_FX_RATES,
    RESPONSE_BAR_PNG,
    RESPONSE_PIE_PNG,
)

_PNG_MAGIC = b"\x89PNG"

_FX = {"JPY": 110.0}


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        REQUEST_EXPENSES: [
            {
                "SK": "EXPENSE#2026-01-01",
                "summary": "Ramen",
                "payment_method": "Cash",
                # A string, as it arrives after crossing the JSON boundary.
                "amount": "1100",
                "currency": "JPY",
                "category": "Food",
                "date": "2026-01-01",
            }
        ],
        REQUEST_FX_RATES: _FX,
    }
    event.update(overrides)
    return event


def test_returns_both_charts_as_base64_pngs() -> None:
    result = lambda_handler(_event(), None)

    assert base64.b64decode(result[RESPONSE_PIE_PNG])[:4] == _PNG_MAGIC
    assert base64.b64decode(result[RESPONSE_BAR_PNG])[:4] == _PNG_MAGIC


def test_renders_placeholders_for_a_trip_with_no_expenses() -> None:
    result = lambda_handler(_event(**{REQUEST_EXPENSES: []}), None)

    assert base64.b64decode(result[RESPONSE_PIE_PNG])[:4] == _PNG_MAGIC
    assert base64.b64decode(result[RESPONSE_BAR_PNG])[:4] == _PNG_MAGIC


def test_amount_as_string_converts_the_same_as_a_decimal_would() -> None:
    # 1100 JPY / 110 = 10 SGD. Proves the string round-trip through JSON is lossless.
    result = lambda_handler(_event(), None)
    assert base64.b64decode(result[RESPONSE_PIE_PNG])[:4] == _PNG_MAGIC


@pytest.mark.parametrize("missing", [REQUEST_EXPENSES, REQUEST_FX_RATES])
def test_rejects_an_event_missing_a_required_key(missing: str) -> None:
    event = _event()
    del event[missing]

    with pytest.raises(ValueError, match=missing):
        lambda_handler(event, None)


def test_rejects_expenses_that_are_not_a_list() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        lambda_handler(_event(**{REQUEST_EXPENSES: {"not": "a list"}}), None)
