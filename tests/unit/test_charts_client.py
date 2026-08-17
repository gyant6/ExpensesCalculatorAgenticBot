"""Tests for the chart Lambda client, covering both the local and production paths."""

from __future__ import annotations

import base64
import io
import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from src.bot import charts_client
from src.bot.chart_protocol import (
    REQUEST_EXPENSES,
    REQUEST_FX_RATES,
    RESPONSE_BAR_PNG,
    RESPONSE_PIE_PNG,
)
from src.bot.charts_client import _to_jsonable, render_charts
from src.bot.config import PRODUCTION_ENVIRONMENT, settings

_PNG_MAGIC = b"\x89PNG"

_FX = {"JPY": 110.0}

_FUNCTION_NAME = "expenses-bot-charts"


def _expense(amount: Any = "1100") -> dict[str, Any]:
    return {
        "SK": "EXPENSE#2026-01-01",
        "summary": "Ramen",
        "payment_method": "Cash",
        "amount": amount,
        "currency": "JPY",
        "category": "Food",
        "date": "2026-01-01",
    }


def _lambda_response(payload: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Build a boto3 invoke() response, whose Payload is a streaming body."""
    return {"Payload": io.BytesIO(json.dumps(payload).encode("utf-8")), **extra}


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", PRODUCTION_ENVIRONMENT)
    monkeypatch.setattr(settings, "CHART_LAMBDA_FUNCTION_NAME", _FUNCTION_NAME)


# ── _to_jsonable ─────────────────────────────────────────────────────────────


def test_decimal_amounts_become_strings_not_floats() -> None:
    # Strings, because to_sgd parses back through Decimal and a float round-trip would
    # reintroduce the representation error the Number migration removed.
    result = _to_jsonable([_expense(amount=Decimal("12.50"))])

    assert result[0]["amount"] == "12.50"
    assert isinstance(result[0]["amount"], str)


def test_non_decimal_fields_are_left_alone() -> None:
    result = _to_jsonable([_expense()])

    assert result[0]["currency"] == "JPY"
    assert result[0]["date"] == "2026-01-01"


def test_the_result_is_json_serialisable() -> None:
    # The failure this guards against is TypeError from json.dumps at the invoke.
    json.dumps(_to_jsonable([_expense(amount=Decimal("1100"))]))


# ── local rendering ──────────────────────────────────────────────────────────


def test_renders_in_process_when_not_in_production() -> None:
    # settings.ENVIRONMENT is whatever .env holds, which is never "production" locally.
    charts = render_charts([_expense()], _FX)

    assert charts is not None
    pie, bar = charts
    assert pie[:4] == _PNG_MAGIC
    assert bar[:4] == _PNG_MAGIC


def test_does_not_invoke_lambda_when_not_in_production() -> None:
    with patch.object(charts_client, "_get_client") as mock_client:
        render_charts([_expense()], _FX)

    mock_client.assert_not_called()


# ── production rendering ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("production")
def test_invokes_the_chart_lambda_and_decodes_both_images() -> None:
    client = MagicMock()
    client.invoke.return_value = _lambda_response(
        {
            RESPONSE_PIE_PNG: base64.b64encode(b"\x89PNGpie").decode("ascii"),
            RESPONSE_BAR_PNG: base64.b64encode(b"\x89PNGbar").decode("ascii"),
        }
    )

    with patch.object(charts_client, "_get_client", return_value=client):
        charts = render_charts([_expense()], _FX)

    assert charts == (b"\x89PNGpie", b"\x89PNGbar")


@pytest.mark.usefixtures("production")
def test_sends_the_expenses_and_rates_in_the_payload() -> None:
    client = MagicMock()
    client.invoke.return_value = _lambda_response(
        {
            RESPONSE_PIE_PNG: base64.b64encode(b"pie").decode("ascii"),
            RESPONSE_BAR_PNG: base64.b64encode(b"bar").decode("ascii"),
        }
    )

    with patch.object(charts_client, "_get_client", return_value=client):
        render_charts([_expense(amount=Decimal("1100"))], _FX)

    kwargs = client.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == _FUNCTION_NAME
    payload = json.loads(kwargs["Payload"])
    assert payload[REQUEST_FX_RATES] == _FX
    assert payload[REQUEST_EXPENSES][0]["amount"] == "1100"


@pytest.mark.usefixtures("production")
def test_returns_none_when_the_function_itself_raised() -> None:
    # A function error still comes back as HTTP 200, so without the FunctionError check
    # a traceback would be base64-decoded as if it were an image.
    client = MagicMock()
    client.invoke.return_value = _lambda_response(
        {"errorMessage": "boom", "errorType": "ValueError"},
        FunctionError="Unhandled",
    )

    with patch.object(charts_client, "_get_client", return_value=client):
        assert render_charts([_expense()], _FX) is None


@pytest.mark.usefixtures("production")
def test_returns_none_when_the_payload_is_missing_an_image() -> None:
    client = MagicMock()
    client.invoke.return_value = _lambda_response(
        {RESPONSE_PIE_PNG: base64.b64encode(b"pie").decode("ascii")}
    )

    with patch.object(charts_client, "_get_client", return_value=client):
        assert render_charts([_expense()], _FX) is None


@pytest.mark.usefixtures("production")
def test_returns_none_when_the_invoke_is_rejected() -> None:
    client = MagicMock()
    client.invoke.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Invoke"
    )

    with patch.object(charts_client, "_get_client", return_value=client):
        assert render_charts([_expense()], _FX) is None


@pytest.mark.usefixtures("production")
def test_returns_none_when_the_invoke_times_out() -> None:
    client = MagicMock()
    client.invoke.side_effect = ReadTimeoutError(endpoint_url="https://lambda")

    with patch.object(charts_client, "_get_client", return_value=client):
        assert render_charts([_expense()], _FX) is None


def test_returns_none_in_production_when_the_function_name_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", PRODUCTION_ENVIRONMENT)
    monkeypatch.setattr(settings, "CHART_LAMBDA_FUNCTION_NAME", None)

    assert render_charts([_expense()], _FX) is None


@pytest.mark.usefixtures("production")
def test_returns_none_when_matplotlib_is_absent_on_the_local_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The production artefact ships charts.py without matplotlib, so taking the local
    # path there raises ImportError. It must degrade, not crash the trip end.
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise ImportError("No module named 'matplotlib'")

    with patch.object(charts_client, "_render_locally", _raise):
        assert render_charts([_expense()], _FX) is None
