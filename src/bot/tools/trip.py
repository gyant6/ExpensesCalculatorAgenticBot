"""LangChain tools for starting and ending an overseas trip."""

import logging
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import ValidationError

from src.bot.export import generate_csv
from src.bot.storage import dynamodb
from src.bot.tools.fx import get_sgd_exchange_rates

logger = logging.getLogger(__name__)

END_TRIP_SUCCESS = "Trip successfully ended."
NO_ACTIVE_TRIP = (
    "There are no active trips to be ended. Start a new trip and add expenses first."
)

# Prefixed to the tool result when rates could not be fetched. Without an explicit
# instruction the model invents an exchange rate to satisfy the system prompt's request
# for an SGD total, putting a fabricated figure in the user's final summary.
FX_UNAVAILABLE_NOTICE = (
    "Exchange rates were unavailable, so the amount_sgd column is blank. "
    "Report each expense in its original currency and give one total per currency. "
    "Do not convert anything to SGD and do not state an SGD total."
)


@tool
def start_trip(
    telegram_user_id: Annotated[str, InjectedState("telegram_user_id")],
) -> str:
    """Start a new overseas trip for the user.

    Creates a TRIP#ACTIVE marker in DynamoDB recording the start date. Only one
    trip can be active at a time. Call this when the user says they are starting
    a trip, going travelling, or similar.

    Args:
        telegram_user_id: The Telegram user ID of the user starting the trip.

    Returns:
        A confirmation string with the start date, or an error string if a trip
        is already active.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    if dynamodb.get_item(f"USER#{telegram_user_id}", "TRIP#ACTIVE"):
        return "There is already an active trip."

    start_date = (datetime.now(tz=ZoneInfo("Asia/Singapore"))).strftime("%Y-%m-%d")
    dynamodb.put_item(
        {
            "PK": f"USER#{telegram_user_id}",
            "SK": "TRIP#ACTIVE",
            "start_date": start_date,
        }
    )

    return f"New trip started on {start_date}."


@tool
def end_trip(
    telegram_user_id: Annotated[str, InjectedState("telegram_user_id")],
) -> str:
    """End the active trip, export its expenses as CSV, and delete all trip records.

    Call this when the user asks to end the trip. Always call get_all_expenses first to
    present the summary, then call this tool. The user will be shown a confirmation
    prompt by the application before this tool actually executes.

    The CSV is built before anything is deleted, so a failure to export leaves the trip
    intact rather than destroying records with no copy of them.

    Args:
        telegram_user_id: The Telegram user ID of the user ending the trip.

    Returns:
        A confirmation line followed by a CSV of every expense in the trip, including an
        amount_sgd column, so the summary can be written from real figures. If exchange
        rates were unavailable, amount_sgd is blank and the CSV is prefixed with an
        instruction not to report SGD figures. Returns an error string if no trip is
        active.

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB request fails.
    """
    pk = f"USER#{telegram_user_id}"
    if dynamodb.get_item(pk, "TRIP#ACTIVE") is None:
        return NO_ACTIVE_TRIP

    expenses = dynamodb.query_by_prefix(pk, "EXPENSE#")

    fx_rates: dict[str, float] = {}
    rates_unavailable = False
    if expenses:
        try:
            fx_rates = get_sgd_exchange_rates()
        except (httpx.HTTPError, RuntimeError, ValidationError):
            logger.exception(
                "FX rate fetch failed for user %s; ending trip without SGD conversion",
                telegram_user_id,
            )
            rates_unavailable = True

    # Export before deleting. If this raises, the tool fails and nothing is removed.
    csv_text = generate_csv(expenses, fx_rates).decode("utf-8")

    for expense in expenses:
        dynamodb.delete_item(pk, expense["SK"])
    dynamodb.delete_item(pk, "TRIP#ACTIVE")

    if rates_unavailable:
        return f"{END_TRIP_SUCCESS}\n\n{FX_UNAVAILABLE_NOTICE}\n\n{csv_text}"
    return f"{END_TRIP_SUCCESS}\n\n{csv_text}"
