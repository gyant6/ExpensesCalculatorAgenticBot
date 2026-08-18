"""Contract tests between charts_client and chart_handler.

The unit tests for each side mock the other, so the two could drift apart — a renamed
payload key, or an amount encoded in a form the handler cannot parse — while both suites
stayed green. These tests wire the real serialiser to the real handler through an actual
JSON round-trip, which is the only part of the split a local Telegram run cannot reach:
locally `render_charts` renders in-process and never builds a payload at all.

What is deliberately not covered here is the boto3 invoke itself. That needs a deployed
function, and it is what the Step 4 smoke test is for.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.bot import charts_client
from src.bot.chart_handler import lambda_handler
from src.bot.chart_protocol import RESPONSE_BAR_PNG, RESPONSE_PIE_PNG
from src.bot.charts_client import render_charts
from src.bot.config import PRODUCTION_ENVIRONMENT, settings

_PNG_MAGIC = b"\x89PNG"

# Rates as get_sgd_exchange_rates returns them: 1 SGD buys this many units.
_FX = {"JPY": 124.0, "VND": 19500.0}

# Expenses as query_by_prefix returns them, with amounts as Decimal because the migration
# made `amount` a DynamoDB Number.
_EXPENSES: list[dict[str, Any]] = [
    {
        "PK": "USER#111",
        "SK": "EXPENSE#2026-01-01T10:00:00+00:00",
        "date": "2026-01-01",
        "summary": "Dinner at Ichiran ramen",
        "category": "Food",
        "amount": Decimal("1500"),
        "currency": "JPY",
        "payment_method": "Cash",
    },
    {
        "PK": "USER#111",
        "SK": "EXPENSE#2026-01-02T09:00:00+00:00",
        "date": "2026-01-02",
        "summary": "Hotel night in Hanoi",
        "category": "Accommodation",
        "amount": Decimal("89000"),
        "currency": "VND",
        "payment_method": "Card",
    },
    {
        "PK": "USER#111",
        "SK": "EXPENSE#2026-01-02T12:00:00+00:00",
        "date": "2026-01-02",
        "summary": "Airport taxi",
        "category": "Transport",
        "amount": Decimal("45.50"),
        "currency": "SGD",
        "payment_method": "Card",
    },
]


def _round_trip(expenses: list[dict[str, Any]], fx_rates: dict[str, float]) -> Any:
    """Send a payload through render_charts into the real handler and back.

    The fake Lambda client stands in only for the transport: the payload it receives is
    the one charts_client built, and the response it returns is the one chart_handler
    produced, both having survived json.dumps and json.loads exactly as they would over
    the wire.
    """
    client = MagicMock()

    def _invoke(**kwargs: Any) -> dict[str, Any]:
        event = json.loads(kwargs["Payload"])
        result = lambda_handler(event, None)
        payload = MagicMock()
        payload.read.return_value = json.dumps(result).encode("utf-8")
        return {"Payload": payload}

    client.invoke.side_effect = _invoke
    with patch.object(charts_client, "_get_client", return_value=client):
        return render_charts(expenses, fx_rates)


@pytest.fixture(autouse=True)
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the invoke path; locally render_charts would never build a payload."""
    monkeypatch.setattr(settings, "ENVIRONMENT", PRODUCTION_ENVIRONMENT)
    monkeypatch.setattr(settings, "CHART_LAMBDA_FUNCTION_NAME", "chart-fn")


def test_a_payload_built_by_the_client_is_accepted_by_the_handler() -> None:
    charts = _round_trip(_EXPENSES, _FX)

    assert charts is not None
    pie, bar = charts
    assert pie[:4] == _PNG_MAGIC
    assert bar[:4] == _PNG_MAGIC


def test_decimal_amounts_survive_the_json_boundary_and_still_convert() -> None:
    # The handler skips any expense it cannot convert, so a Decimal that arrived in an
    # unusable form would silently produce an empty chart rather than an error. Compare
    # against the same expenses with amounts already stringified: if the Decimal encoding
    # were lossy or unparseable, the two renders would differ.
    stringified = [{**e, "amount": str(e["amount"])} for e in _EXPENSES]

    from_decimals = _round_trip(_EXPENSES, _FX)
    from_strings = _round_trip(stringified, _FX)

    assert from_decimals == from_strings


def test_the_handler_response_keys_are_the_ones_the_client_reads() -> None:
    # Named explicitly so a rename on either side fails here rather than degrading to a
    # silently missing chart in production.
    result = lambda_handler(
        {"expenses": [{**_EXPENSES[0], "amount": "1500"}], "fx_rates": _FX}, None
    )

    assert set(result) == {RESPONSE_PIE_PNG, RESPONSE_BAR_PNG}
    assert base64.b64decode(result[RESPONSE_PIE_PNG])[:4] == _PNG_MAGIC


def test_a_trip_with_no_expenses_round_trips_to_placeholder_charts() -> None:
    charts = _round_trip([], _FX)

    assert charts is not None
    pie, bar = charts
    assert pie[:4] == _PNG_MAGIC
    assert bar[:4] == _PNG_MAGIC


def test_an_expense_with_no_rate_does_not_break_the_round_trip() -> None:
    unconvertible = [{**_EXPENSES[0], "currency": "EUR"}]

    charts = _round_trip(unconvertible, _FX)

    assert charts is not None
