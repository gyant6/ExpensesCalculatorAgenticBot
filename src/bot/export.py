"""CSV export and SGD conversion for trip expense summaries.

Deliberately free of matplotlib. `end_trip` builds its CSV here and runs inside the main
Lambda, so anything this module imports is imported on every cold start of that
function — including one that only records an expense. The plotting code that used to
live alongside these functions is in `charts.py`, which is deployed separately.
"""

from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

CSV_FIELDNAMES = [
    "date",
    "summary",
    "category",
    "amount",
    "currency",
    "amount_sgd",
    "payment_method",
]


def to_sgd(expense: dict[str, Any], fx_rates: dict[str, float]) -> float | None:
    """Convert one expense's amount to SGD.

    Shared by the CSV export and the chart rendering so the two cannot disagree on what
    an expense is worth.

    Args:
        expense: Expense dict as returned by `query_by_prefix`. Reads `amount` and
            `currency`, and `SK` only to identify the row in a warning.
        fx_rates: Exchange rates with SGD as base (e.g. {'JPY': 167.5}). A rate of R
            means 1 SGD buys R units of that currency, so conversion divides by it.

    Returns:
        The SGD equivalent, or None if the amount is unparseable or no rate is
        available for the currency. Both cases are logged and left for the caller to
        represent — the CSV blanks the column, the charts omit the expense.
    """
    currency = expense.get("currency", "")
    try:
        amount = float(Decimal(expense["amount"]))
    except (InvalidOperation, KeyError):
        logger.warning("Skipping expense with invalid amount: %s", expense.get("SK"))
        return None

    if currency == "SGD":
        return amount

    rate = fx_rates.get(currency)
    if not rate:
        logger.warning("No FX rate for %s — expense skipped in chart", currency)
        return None

    return amount / rate


def generate_csv(expenses: list[dict[str, Any]], fx_rates: dict[str, float]) -> bytes:
    """Generate a UTF-8 CSV of all expenses for the trip.

    Columns: date, summary, category, amount, currency, amount_sgd, payment_method.
    amount_sgd is the SGD equivalent rounded to 2 decimal places; blank if the
    currency rate is unavailable.

    Args:
        expenses: List of expense dicts as returned by query_by_prefix.
        fx_rates: Exchange rates with SGD as base, used to populate amount_sgd.

    Returns:
        CSV content as UTF-8 encoded bytes, suitable for sending as a file attachment.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=CSV_FIELDNAMES,
        extrasaction="ignore",
    )
    writer.writeheader()
    for expense in expenses:
        sgd = to_sgd(expense, fx_rates)
        writer.writerow(
            {
                **expense,
                "amount_sgd": f"{sgd:.2f}" if sgd is not None else "",
            }
        )
    return buf.getvalue().encode("utf-8")
