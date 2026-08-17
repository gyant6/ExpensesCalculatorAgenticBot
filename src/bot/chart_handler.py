"""Entrypoint for the chart Lambda.

Deployed as a separate function from the bot so that matplotlib, numpy, Pillow and
fontTools stay out of the main artefact. It is a pure function of its input: expenses and
exchange rates in, PNG bytes out. It reads no database, holds no state and needs no
credentials beyond writing its own logs, which is what makes it separable at all.

Invoked synchronously by `charts_client.render_charts`, once per trip end.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from src.bot.chart_protocol import (
    REQUEST_EXPENSES,
    REQUEST_FX_RATES,
    RESPONSE_BAR_PNG,
    RESPONSE_PIE_PNG,
)
from src.bot.charts import generate_charts

# Read straight from the environment rather than through config.py. Settings requires the
# bot token and admin ID, and loads them from SSM in production — none of which this
# function has, or should have.
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").strip().upper())
logger = logging.getLogger(__name__)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, str]:
    """Render a trip's pie and bar charts.

    Args:
        event: Invocation payload with two keys. `expenses` is the list of expense items
            for the trip, with amounts as strings rather than Decimals because they
            crossed a JSON boundary. `fx_rates` maps currency codes to their rate against
            SGD.
        context: Lambda runtime context. Unused; charts depend only on the payload.

    Returns:
        Dict with `pie_png_base64` and `bar_png_base64`, each a base64-encoded PNG.
        Base64 because a Lambda response is JSON and cannot carry raw bytes.

    Raises:
        ValueError: If either required key is absent from the event, or if `expenses` is
            not a list. Raising surfaces as a FunctionError to the caller, which logs it
            and delivers the trip summary without charts.
    """
    if REQUEST_EXPENSES not in event or REQUEST_FX_RATES not in event:
        raise ValueError(
            f"Event must contain {REQUEST_EXPENSES!r} and {REQUEST_FX_RATES!r}; "
            f"got {sorted(event)}"
        )

    expenses = event[REQUEST_EXPENSES]
    fx_rates = event[REQUEST_FX_RATES]
    if not isinstance(expenses, list):
        raise ValueError(
            f"{REQUEST_EXPENSES!r} must be a list, got {type(expenses).__name__}"
        )

    logger.info("Rendering charts for %d expenses", len(expenses))
    pie_bytes, bar_bytes = generate_charts(expenses, fx_rates)

    return {
        RESPONSE_PIE_PNG: base64.b64encode(pie_bytes).decode("ascii"),
        RESPONSE_BAR_PNG: base64.b64encode(bar_bytes).decode("ascii"),
    }
