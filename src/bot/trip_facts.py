"""Figures about a finished trip, computed in code from the delivered CSV.

The trip-insights model is only ever given what this module computes, and its reply is
checked against it, so no figure in the insights message comes from the model's own
arithmetic. Built from the same CSV the user receives, so the figures agree with it.

Deliberately free of AWS and matplotlib imports; it is imported on the main function's
cold start.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from src.bot.export import CSV_FIELDNAMES, money_cell

# How many of the largest expenses are listed individually.
TOP_EXPENSE_COUNT: Final = 5

# Longest expense summary passed on. The summary is text the user typed, so it is cut
# down and cleaned before it reaches a model prompt.
_SUMMARY_MAX_CHARS: Final = 60

_WHITESPACE: Final = re.compile(r"\s+")
_ANGLE_BRACKETS: Final = re.compile(r"[<>]")
_PERCENT: Final = Decimal(100)
_WHOLE_PERCENT: Final = Decimal(1)


@dataclass(frozen=True)
class CategoryFact:
    """Spending in one category, in SGD."""

    category: str
    total: Decimal
    percent: Decimal
    expense_count: int


@dataclass(frozen=True)
class DayFact:
    """Spending on one calendar day, in SGD."""

    day: date
    total: Decimal
    expense_count: int


@dataclass(frozen=True)
class ExpenseFact:
    """One expense, with its converted and original amounts."""

    summary: str
    category: str
    day: date
    total: Decimal
    amount: Decimal
    currency: str
    payment_method: str


@dataclass(frozen=True)
class CurrencyFact:
    """Spending in one original currency."""

    currency: str
    amount: Decimal
    total: Decimal
    expense_count: int


@dataclass(frozen=True)
class PaymentFact:
    """Spending by one payment method, in SGD."""

    payment_method: str
    total: Decimal
    expense_count: int


@dataclass(frozen=True)
class TripFacts:
    """Everything the insights model may talk about, all computed from the CSV.

    Attributes:
        expense_count: Number of expenses on the trip.
        total: Trip total in SGD, the sum of the CSV's rounded amount_sgd cells.
        first_day: Date of the earliest expense.
        last_day: Date of the latest expense.
        day_count: Calendar days from first_day to last_day inclusive.
        average_per_day: total divided by day_count, or None for a one-day trip.
        categories: Per-category spending, largest first.
        days: Per-day spending, in date order.
        top_expenses: The largest expenses, largest first.
        currencies: Per-currency spending, largest SGD amount first.
        payment_methods: Per-payment-method spending, largest first.
    """

    expense_count: int
    total: Decimal
    first_day: date
    last_day: date
    day_count: int
    average_per_day: Decimal | None
    categories: tuple[CategoryFact, ...]
    days: tuple[DayFact, ...]
    top_expenses: tuple[ExpenseFact, ...]
    currencies: tuple[CurrencyFact, ...]
    payment_methods: tuple[PaymentFact, ...]

    def to_prompt_text(self) -> str:
        """Render the facts as labelled plain-text lines for the insights prompt.

        Every figure the model may quote appears here exactly as it should be quoted,
        and the same text is the allow-list the model's reply is checked against.

        Returns:
            The fact sheet, one fact per line.
        """
        busiest = max(self.days, key=lambda fact: fact.total)
        lines = [
            f"Trip: {self.first_day.isoformat()} to {self.last_day.isoformat()}, "
            f"{self.day_count} {_plural(self.day_count, 'day')}, "
            f"{self.expense_count} {_plural(self.expense_count, 'expense')}",
            f"Total: SGD {self.total:,.2f}",
        ]
        if self.average_per_day is not None:
            lines.append(f"Average per day: SGD {self.average_per_day:,.2f}")
        lines.append(
            f"Largest category: {self.categories[0].category} "
            f"({self.categories[0].percent:.0f}% of the total)"
        )
        lines.append(
            f"Busiest day: {busiest.day.isoformat()}, SGD {busiest.total:,.2f}, "
            f"{busiest.expense_count} {_plural(busiest.expense_count, 'expense')}"
        )
        if len(self.categories) >= 2:
            first, second = self.categories[:2]
            together = _percent_of(first.total + second.total, self.total)
            lines.append(
                f"Top two categories together: {first.category} and "
                f"{second.category}, {together:.0f}% of the total"
            )
        lines.append("Categories, largest first:")
        lines.extend(
            f"  {fact.category}: SGD {fact.total:,.2f}, {fact.percent:.0f}%, "
            f"{fact.expense_count} {_plural(fact.expense_count, 'expense')}"
            for fact in self.categories
        )
        lines.append("Days:")
        lines.extend(
            f"  {fact.day.isoformat()}: SGD {fact.total:,.2f}, "
            f"{fact.expense_count} {_plural(fact.expense_count, 'expense')}"
            for fact in self.days
        )
        lines.append("Largest expenses, largest first:")
        lines.extend(
            f"  {fact.summary} ({fact.category}, {fact.day.isoformat()}): "
            f"SGD {fact.total:,.2f}, {_percent_of(fact.total, self.total):.0f}% of "
            f"the total, paid {fact.amount:,.2f} {fact.currency} by "
            f"{fact.payment_method}"
            for fact in self.top_expenses
        )
        lines.append("Currencies paid in:")
        lines.extend(
            f"  {fact.currency}: {fact.amount:,.2f} {fact.currency} = "
            f"SGD {fact.total:,.2f}, {fact.expense_count} "
            f"{_plural(fact.expense_count, 'expense')}"
            for fact in self.currencies
        )
        lines.append("Payment methods:")
        lines.extend(
            f"  {fact.payment_method}: SGD {fact.total:,.2f}, {fact.expense_count} "
            f"{_plural(fact.expense_count, 'expense')}"
            for fact in self.payment_methods
        )
        return "\n".join(lines)


def _plural(count: int, noun: str) -> str:
    """The noun, pluralised for the count."""
    return noun if count == 1 else f"{noun}s"


def _clean_text(raw: str) -> str:
    """Make user-typed text safe to place in a prompt.

    Collapses all whitespace, so a newline cannot start a fake instruction line, strips
    angle brackets, so the text cannot close the data fence it sits in, and truncates.
    Applied to every free-text field of an expense, not only its summary.

    Args:
        raw: An expense summary, currency or payment method as stored.

    Returns:
        A single line of at most _SUMMARY_MAX_CHARS characters.
    """
    single_line = _WHITESPACE.sub(" ", _ANGLE_BRACKETS.sub(" ", raw)).strip()
    return single_line[:_SUMMARY_MAX_CHARS].rstrip()


def _percent_of(part: Decimal, whole: Decimal) -> Decimal:
    """part as a whole-number percentage of whole, rounded half up."""
    return (part / whole * _PERCENT).quantize(_WHOLE_PERCENT, rounding=ROUND_HALF_UP)


def compute_trip_facts(csv_bytes: bytes) -> TripFacts | None:
    """Compute a trip's facts from the exact CSV delivered to the user.

    Args:
        csv_bytes: UTF-8 output from generate_csv.

    Returns:
        The trip's facts, or None when there is nothing to analyse: no expenses, or
        any expense without an SGD conversion, since a partial total would mislead.

    Raises:
        ValueError: If the CSV has unexpected columns, or a row has an invalid amount or
            date.
    """
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    if reader.fieldnames != CSV_FIELDNAMES:
        raise ValueError("Trip CSV has unexpected columns")
    rows = list(reader)
    if not rows or not all(row["amount_sgd"] for row in rows):
        return None

    expenses = [
        ExpenseFact(
            summary=_clean_text(row["summary"]),
            category=_clean_text(row["category"]),
            day=date.fromisoformat(row["date"]),
            total=money_cell(row["amount_sgd"]),
            amount=money_cell(row["amount"]),
            currency=_clean_text(row["currency"]),
            payment_method=_clean_text(row["payment_method"]),
        )
        for row in rows
    ]
    total = sum((expense.total for expense in expenses), Decimal(0))
    first_day = min(expense.day for expense in expenses)
    last_day = max(expense.day for expense in expenses)
    day_count = (last_day - first_day).days + 1

    by_category: defaultdict[str, list[ExpenseFact]] = defaultdict(list)
    by_day: defaultdict[date, list[ExpenseFact]] = defaultdict(list)
    by_currency: defaultdict[str, list[ExpenseFact]] = defaultdict(list)
    by_payment: defaultdict[str, list[ExpenseFact]] = defaultdict(list)
    for expense in expenses:
        by_category[expense.category].append(expense)
        by_day[expense.day].append(expense)
        by_currency[expense.currency].append(expense)
        by_payment[expense.payment_method].append(expense)

    def subtotal(group: list[ExpenseFact]) -> Decimal:
        return sum((expense.total for expense in group), Decimal(0))

    return TripFacts(
        expense_count=len(expenses),
        total=total,
        first_day=first_day,
        last_day=last_day,
        day_count=day_count,
        average_per_day=(
            (total / day_count).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if day_count > 1
            else None
        ),
        categories=tuple(
            CategoryFact(
                category=name,
                total=subtotal(group),
                percent=_percent_of(subtotal(group), total),
                expense_count=len(group),
            )
            for name, group in sorted(
                by_category.items(), key=lambda item: (-subtotal(item[1]), item[0])
            )
        ),
        days=tuple(
            DayFact(day=day, total=subtotal(group), expense_count=len(group))
            for day, group in sorted(by_day.items())
        ),
        top_expenses=tuple(
            sorted(expenses, key=lambda e: (-e.total, e.day, e.summary))[
                :TOP_EXPENSE_COUNT
            ]
        ),
        currencies=tuple(
            CurrencyFact(
                currency=name,
                amount=sum((expense.amount for expense in group), Decimal(0)),
                total=subtotal(group),
                expense_count=len(group),
            )
            for name, group in sorted(
                by_currency.items(), key=lambda item: (-subtotal(item[1]), item[0])
            )
        ),
        payment_methods=tuple(
            PaymentFact(
                payment_method=name, total=subtotal(group), expense_count=len(group)
            )
            for name, group in sorted(
                by_payment.items(), key=lambda item: (-subtotal(item[1]), item[0])
            )
        ),
    )
