"""Pie and bar chart rendering for trip expense summaries.

This module imports matplotlib, which pulls in numpy, Pillow and fontTools — together
the largest part of any artefact containing them. It is therefore deployed only in the
chart Lambda and must not be imported by anything reachable from the main function's
handler. The CSV export and the SGD conversion both callers need live in `export.py`,
which has no such dependency.
"""

from __future__ import annotations

import io
import threading
from collections import defaultdict
from typing import Any

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from src.bot.export import to_sgd

# Headless rendering: no display is available in Lambda or in CI. Set before any figure
# is created, which only happens inside the functions below.
matplotlib.use("Agg")

# pyplot keeps figure state in module-level globals, so two trips ending at once must not
# render concurrently. Lives here rather than at the call site because it guards this
# module's state, and every caller needs the same protection.
_PYPLOT_LOCK = threading.Lock()

# Categorical palette — light mode, fixed order (dataviz reference)
_COLORS = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]

_SURFACE = "#fcfcfb"
_INK_PRIMARY = "#0b0b0b"
_INK_SECONDARY = "#52514e"
_GRIDLINE = "#e1e0d9"


def generate_charts(
    expenses: list[dict[str, Any]],
    fx_rates: dict[str, float],
) -> tuple[bytes, bytes]:
    """Generate a pie chart (by category) and bar chart (by day) for a trip.

    All amounts are converted to SGD using the provided exchange rates.
    Expenses with unknown or missing currency rates are logged and skipped.

    Args:
        expenses: List of expense dicts with keys: amount, currency, category, date.
        fx_rates: Exchange rates with SGD as base (e.g. {'VND': 17500.0, 'USD': 0.74}).
                  A rate of R means 1 SGD = R units of that currency.

    Returns:
        Tuple of (pie_chart_bytes, bar_chart_bytes) as PNG bytes.
    """
    category_totals: dict[str, float] = defaultdict(float)
    date_totals: dict[str, float] = defaultdict(float)

    for expense in expenses:
        sgd = to_sgd(expense, fx_rates)
        if sgd is None:
            continue
        category_totals[expense["category"]] += sgd
        date_totals[expense["date"]] += sgd

    with _PYPLOT_LOCK:
        return _pie_chart(category_totals), _bar_chart(date_totals)


def _pie_chart(category_totals: dict[str, float]) -> bytes:
    """Render a pie chart of spending by category. Returns PNG bytes."""
    if not category_totals:
        return _placeholder("No expenses to display")

    labels = list(category_totals.keys())
    values = [category_totals[label] for label in labels]
    colors = [_COLORS[i % len(_COLORS)] for i in range(len(labels))]
    total = sum(values)

    fig, ax = plt.subplots(figsize=(7, 5), facecolor=_SURFACE)
    ax.set_facecolor(_SURFACE)

    _, texts, autotexts = ax.pie(
        values,
        labels=labels,
        colors=colors,
        autopct="%1.1f%%",
        pctdistance=0.8,
        wedgeprops={"linewidth": 2, "edgecolor": _SURFACE},
        textprops={"color": _INK_PRIMARY, "fontsize": 9},
    )
    for at in autotexts:
        at.set_color(_INK_PRIMARY)
        at.set_fontsize(8)

    ax.set_title(
        f"Spending by Category  ·  SGD {total:.2f} total",
        color=_INK_PRIMARY,
        fontsize=11,
        pad=12,
    )

    return _to_bytes(fig)


def _bar_chart(date_totals: dict[str, float]) -> bytes:
    """Render a bar chart of daily spending in SGD. Returns PNG bytes."""
    if not date_totals:
        return _placeholder("No expenses to display")

    dates = sorted(date_totals.keys())
    values = [date_totals[d] for d in dates]
    max_val = max(values)
    width = max(5.0, len(dates) * 0.9 + 2)

    fig, ax = plt.subplots(figsize=(width, 4), facecolor=_SURFACE)
    ax.set_facecolor(_SURFACE)

    bars = ax.bar(dates, values, color=_COLORS[0], width=0.6)

    for bar, val in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + max_val * 0.01,
            f"{val:.0f}",
            ha="center",
            va="bottom",
            fontsize=8,
            color=_INK_SECONDARY,
        )

    ax.set_xlabel("Date", color=_INK_SECONDARY, fontsize=9)
    ax.set_ylabel("SGD", color=_INK_SECONDARY, fontsize=9)
    ax.set_title("Daily Spending (SGD)", color=_INK_PRIMARY, fontsize=11, pad=12)
    ax.tick_params(colors=_INK_SECONDARY, labelsize=8)
    plt.xticks(rotation=30, ha="right")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(_GRIDLINE)
    ax.spines["bottom"].set_color(_GRIDLINE)
    ax.yaxis.grid(True, color=_GRIDLINE, linewidth=0.5, zorder=0)
    ax.set_axisbelow(True)

    fig.tight_layout()
    return _to_bytes(fig)


def _to_bytes(fig: Figure) -> bytes:
    """Serialise a matplotlib figure to PNG bytes and close it."""
    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor()
    )
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _placeholder(message: str) -> bytes:
    """Return a minimal PNG with a plain text message for edge cases."""
    fig, ax = plt.subplots(figsize=(4, 2), facecolor=_SURFACE)
    ax.set_facecolor(_SURFACE)
    ax.text(0.5, 0.5, message, ha="center", va="center", color=_INK_SECONDARY)
    ax.axis("off")
    return _to_bytes(fig)
