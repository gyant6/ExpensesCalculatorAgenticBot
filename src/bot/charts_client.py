"""Chart rendering entry point for the main Lambda.

Charts are the only reason matplotlib, numpy, Pillow and fontTools would appear in an
artefact, and together they are larger than everything else the bot depends on. They are
therefore rendered by a separate Lambda, invoked once per trip end, so the main function
never imports them — not on a trip end, and not on the cold start of a message that only
records an expense.

Locally there is no second function to invoke, so rendering happens in-process against
the matplotlib installed in the dev environment. `ENVIRONMENT` selects between the two.
"""

from __future__ import annotations

import base64
import json
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.bot.chart_protocol import (
    REQUEST_EXPENSES,
    REQUEST_FX_RATES,
    RESPONSE_BAR_PNG,
    RESPONSE_PIE_PNG,
)
from src.bot.config import PRODUCTION_ENVIRONMENT, settings

if TYPE_CHECKING:
    from mypy_boto3_lambda import LambdaClient

logger = logging.getLogger(__name__)

# Field Lambda sets when the function itself raised, as opposed to the invoke failing.
# A function error still returns HTTP 200 with the traceback as the payload, so it has to
# be checked explicitly or a stack trace would be decoded as if it were an image.
_FUNCTION_ERROR_KEY: Final = "FunctionError"

# Final rather than a bare assignment so the type narrows to the literal, which is what
# the Lambda client's InvocationType accepts. Without it the constant widens to str and a
# typo here would only surface as a failed invoke at trip end.
_INVOCATION_TYPE_SYNCHRONOUS: Final = "RequestResponse"


def _get_client() -> LambdaClient:
    """Create a Lambda client with timeouts and a bounded retry policy.

    botocore's defaults allow a read to hang for a minute, which would burn the main
    function's own timeout while the user waits on a trip summary. Returned fresh on each
    call so tests can patch it.

    Returns:
        A boto3 Lambda client scoped to the configured region.
    """
    return boto3.client(
        "lambda",
        region_name=settings.AWS_REGION,
        config=Config(
            connect_timeout=5,
            read_timeout=settings.CHART_LAMBDA_TIMEOUT_SECONDS,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _to_jsonable(expenses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert DynamoDB-derived expense items into something json.dumps accepts.

    Amounts come back from DynamoDB as `Decimal`, which the JSON encoder refuses. They
    are converted to strings rather than floats because `to_sgd` parses them back through
    `Decimal`, and a float round-trip would reintroduce the representation error the
    Number migration removed.

    Args:
        expenses: Expense items as returned by `query_by_prefix`.

    Returns:
        The same items with every Decimal value rendered as a string.
    """
    return [
        {k: str(v) if isinstance(v, Decimal) else v for k, v in expense.items()}
        for expense in expenses
    ]


def _render_locally(
    expenses: list[dict[str, Any]], fx_rates: dict[str, float]
) -> tuple[bytes, bytes]:
    """Render both charts in this process, for local development.

    Args:
        expenses: Expense items for the trip.
        fx_rates: Exchange rates with SGD as base.

    Returns:
        Tuple of (pie_chart_png, bar_chart_png).

    Raises:
        ImportError: If matplotlib is not installed, which is the case in the main
            function's production artefact.
    """
    # Imported inside the function deliberately. charts.py ships in the main artefact as
    # source, but matplotlib does not, so a module-level import here would work on a dev
    # machine and then fail at cold start in production.
    from src.bot.charts import generate_charts

    return generate_charts(expenses, fx_rates)


def _invoke_chart_lambda(
    expenses: list[dict[str, Any]], fx_rates: dict[str, float]
) -> tuple[bytes, bytes]:
    """Render both charts by invoking the chart Lambda synchronously.

    Args:
        expenses: Expense items for the trip.
        fx_rates: Exchange rates with SGD as base.

    Returns:
        Tuple of (pie_chart_png, bar_chart_png).

    Raises:
        RuntimeError: If CHART_LAMBDA_FUNCTION_NAME is unset, if the function reported an
            error, or if the response did not contain both images.
        botocore.exceptions.ClientError: If the invoke was rejected.
        botocore.exceptions.BotoCoreError: If the invoke timed out or could not connect.
    """
    function_name = settings.CHART_LAMBDA_FUNCTION_NAME
    if not function_name:
        raise RuntimeError(
            "CHART_LAMBDA_FUNCTION_NAME is not set, so charts cannot be rendered in "
            f"ENVIRONMENT={PRODUCTION_ENVIRONMENT}."
        )

    response = _get_client().invoke(
        FunctionName=function_name,
        InvocationType=_INVOCATION_TYPE_SYNCHRONOUS,
        Payload=json.dumps(
            {
                REQUEST_EXPENSES: _to_jsonable(expenses),
                REQUEST_FX_RATES: fx_rates,
            }
        ).encode("utf-8"),
    )

    payload = json.loads(response["Payload"].read())

    if response.get(_FUNCTION_ERROR_KEY):
        raise RuntimeError(
            f"Chart Lambda {function_name} failed: {payload.get('errorMessage', payload)}"
        )

    try:
        pie = base64.b64decode(payload[RESPONSE_PIE_PNG])
        bar = base64.b64decode(payload[RESPONSE_BAR_PNG])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Chart Lambda {function_name} returned an unusable payload: "
            f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}"
        ) from exc

    return pie, bar


def render_charts(
    expenses: list[dict[str, Any]], fx_rates: dict[str, float]
) -> tuple[bytes, bytes] | None:
    """Render the trip's pie and bar charts.

    Invokes the chart Lambda in production and renders in-process everywhere else.
    Blocking either way, so callers should run it in a worker thread.

    Every failure is logged and swallowed. Charts are a convenience: the authoritative
    record of a trip is the CSV that `end_trip` returns, and a chart that cannot be drawn
    must not cost the user their summary.

    Args:
        expenses: Expense items for the trip, as returned by `query_by_prefix`.
        fx_rates: Exchange rates with SGD as base. Charts plot SGD only, so callers
            should skip this entirely when rates are unavailable.

    Returns:
        Tuple of (pie_chart_png, bar_chart_png), or None if rendering failed.
    """
    try:
        if settings.ENVIRONMENT == PRODUCTION_ENVIRONMENT:
            return _invoke_chart_lambda(expenses, fx_rates)
        return _render_locally(expenses, fx_rates)
    except (ClientError, BotoCoreError, RuntimeError, ValueError, ImportError):
        logger.exception("Chart rendering failed; continuing without charts")
        return None
