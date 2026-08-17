"""Tests for pie and bar chart rendering in charts.py."""

from __future__ import annotations

from src.bot.charts import generate_charts

_PNG_MAGIC = b"\x89PNG"

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


def test_generate_charts_returns_valid_png_bytes() -> None:
    expenses = [
        _expense(amount="12", currency="SGD", category="Food", date="2026-01-01"),
        _expense(amount="550", currency="JPY", category="Transport", date="2026-01-02"),
    ]
    pie, bar = generate_charts(expenses, _FX)
    assert pie[:4] == _PNG_MAGIC
    assert bar[:4] == _PNG_MAGIC


def test_generate_charts_with_empty_expenses_returns_placeholder_pngs() -> None:
    pie, bar = generate_charts([], _FX)
    assert pie[:4] == _PNG_MAGIC
    assert bar[:4] == _PNG_MAGIC


def test_generate_charts_skips_expenses_with_no_convertible_rate() -> None:
    # All expenses have unknown currency — charts should render placeholders
    expenses = [_expense(amount="100", currency="EUR")]
    pie, bar = generate_charts(expenses, _FX)
    assert pie[:4] == _PNG_MAGIC
    assert bar[:4] == _PNG_MAGIC


def test_generate_charts_single_day_single_category() -> None:
    pie, bar = generate_charts([_expense(amount="20", currency="SGD")], _FX)
    assert pie[:4] == _PNG_MAGIC
    assert bar[:4] == _PNG_MAGIC
