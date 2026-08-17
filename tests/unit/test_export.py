"""Tests for SGD conversion and CSV export in export.py."""

from __future__ import annotations

import csv
import io

import pytest

from src.bot.export import CSV_FIELDNAMES, generate_csv, to_sgd

_FX = {"JPY": 110.0, "USD": 0.74, "VND": 17500.0}


def _expense(
    amount: str = "10",
    currency: str = "SGD",
    category: str = "Food",
    date: str = "2026-01-01",
    **extra: str,
) -> dict[str, str]:
    return {
        "SK": "EXPENSE#2026-01-01",
        "summary": "Test expense",
        "payment_method": "Cash",
        "amount": amount,
        "currency": currency,
        "category": category,
        "date": date,
        **extra,
    }


def _parse_csv(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))


# ── to_sgd ───────────────────────────────────────────────────────────────────


def test_to_sgd_returns_amount_directly_for_sgd() -> None:
    assert to_sgd(_expense(amount="12.50", currency="SGD"), _FX) == pytest.approx(12.50)


def test_to_sgd_converts_foreign_currency() -> None:
    # 1100 JPY / 110 = 10 SGD
    assert to_sgd(_expense(amount="1100", currency="JPY"), _FX) == pytest.approx(10.0)


def test_to_sgd_returns_none_when_rate_missing() -> None:
    assert to_sgd(_expense(amount="100", currency="EUR"), _FX) is None


def test_to_sgd_returns_none_for_invalid_amount() -> None:
    assert to_sgd(_expense(amount="abc", currency="SGD"), _FX) is None


def test_to_sgd_returns_none_when_amount_key_missing() -> None:
    expense = _expense()
    del expense["amount"]
    assert to_sgd(expense, _FX) is None


def test_to_sgd_returns_none_for_empty_fx_rates_on_foreign_currency() -> None:
    assert to_sgd(_expense(amount="1000", currency="JPY"), {}) is None


# ── generate_csv ─────────────────────────────────────────────────────────────


def test_generate_csv_produces_correct_columns() -> None:
    rows = _parse_csv(generate_csv([_expense()], _FX))
    assert list(rows[0].keys()) == CSV_FIELDNAMES


def test_generate_csv_sgd_expense_has_correct_amount_sgd() -> None:
    rows = _parse_csv(generate_csv([_expense(amount="15", currency="SGD")], _FX))
    assert rows[0]["amount_sgd"] == "15.00"


def test_generate_csv_foreign_expense_converts_amount_sgd() -> None:
    # 550 JPY / 110 = 5 SGD
    rows = _parse_csv(generate_csv([_expense(amount="550", currency="JPY")], _FX))
    assert rows[0]["amount_sgd"] == "5.00"


def test_generate_csv_leaves_amount_sgd_blank_when_rate_missing() -> None:
    rows = _parse_csv(generate_csv([_expense(amount="50", currency="EUR")], _FX))
    assert rows[0]["amount_sgd"] == ""


def test_generate_csv_empty_expenses_produces_header_only() -> None:
    rows = _parse_csv(generate_csv([], _FX))
    assert rows == []


def test_generate_csv_multiple_expenses() -> None:
    expenses = [
        _expense(amount="10", currency="SGD", date="2026-01-01"),
        _expense(amount="220", currency="JPY", date="2026-01-02"),
    ]
    rows = _parse_csv(generate_csv(expenses, _FX))
    assert len(rows) == 2
    assert rows[0]["amount_sgd"] == "10.00"
    assert rows[1]["amount_sgd"] == "2.00"


def test_generate_csv_preserves_original_amount_and_currency() -> None:
    rows = _parse_csv(generate_csv([_expense(amount="8000", currency="JPY")], _FX))
    assert rows[0]["amount"] == "8000"
    assert rows[0]["currency"] == "JPY"
