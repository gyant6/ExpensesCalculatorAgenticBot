"""LangChain tools for starting and ending an overseas trip."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import ValidationError

from src.bot.config import settings
from src.bot.export import generate_csv
from src.bot.storage import dynamodb
from src.bot.timezones import describe_date, is_valid_timezone, local_date
from src.bot.tools.expenses import list_expenses
from src.bot.tools.fx import get_sgd_exchange_rates

logger = logging.getLogger(__name__)

# Sort-key prefix of an ended trip's archive item. Deliberately not EXPENSE# or TRIP#, so
# no query for the current trip's expenses or its active marker can ever match it.
ARCHIVE_SK_PREFIX = "ARCHIVE#"

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


def _invalid_timezone(name: str) -> str:
    """Error string for a time zone name zoneinfo does not recognise."""
    return (
        f"{name!r} is not a valid IANA time zone name. Use a name such as "
        "'Asia/Tokyo', 'Europe/London' or 'America/Los_Angeles'."
    )


@tool
def start_trip(
    ledger_id: Annotated[str, InjectedState("ledger_id")],
    message_time: Annotated[str, InjectedState("message_time")],
    timezone: str,
) -> str:
    """Start a new overseas trip for the user, in the time zone they are travelling to.

    Creates a TRIP#ACTIVE marker recording the start date and the trip's time zone. Only
    one trip can be active at a time. Call this when the user says they are starting a
    trip, going travelling, or similar. If they have not said where they are going, ask
    once before calling this — the time zone decides which day every expense lands on.

    Args:
        timezone: IANA time zone name of where the user is travelling, e.g. 'Asia/Tokyo'
            for Japan or 'America/Los_Angeles' for California.

    Returns:
        A confirmation with the start date and time zone, or an error string if a trip
        is already active or the time zone name is not valid.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    if dynamodb.get_item(f"USER#{ledger_id}", "TRIP#ACTIVE"):
        return "There is already an active trip."
    if not is_valid_timezone(timezone):
        return _invalid_timezone(timezone)

    start_date = local_date(message_time, timezone)
    dynamodb.put_item(
        {
            "PK": f"USER#{ledger_id}",
            "SK": "TRIP#ACTIVE",
            "start_date": start_date,
            "timezone": timezone,
        }
    )
    return f"New trip started on {start_date} ({timezone})."


@tool
def set_trip_timezone(
    ledger_id: Annotated[str, InjectedState("ledger_id")],
    message_time: Annotated[str, InjectedState("message_time")],
    timezone: str,
) -> str:
    """Change the active trip's time zone when the user moves somewhere new.

    Call this when the user says they are now somewhere with a different time zone —
    e.g. "I'm in Seoul now", "we've landed in London". Expenses already recorded keep
    their dates; new ones, and "today" from now on, follow the new zone.

    Args:
        timezone: IANA time zone name of where the user now is, e.g. 'Asia/Seoul'.

    Returns:
        A confirmation with today's date in the new zone, or an error string if no trip
        is active or the time zone name is not valid.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails, including when
            the trip is ended between being read and being updated.
    """
    if not is_valid_timezone(timezone):
        return _invalid_timezone(timezone)
    pk = f"USER#{ledger_id}"
    if dynamodb.get_item(pk, "TRIP#ACTIVE") is None:
        return "There is no active trip. Start a trip first."

    dynamodb.update_item(pk, "TRIP#ACTIVE", {"timezone": timezone})
    today = describe_date(local_date(message_time, timezone))
    return f"Time zone set to {timezone}. Today there is {today}."


def _archive_trip(pk: str, start_date: str, expenses: list[dict[str, Any]]) -> None:
    """Copy an ended trip's expenses into one archive item that expires on its own.

    One item rather than one per expense, so the archive is written by a single put that
    either lands whole or not at all. DynamoDB caps an item at 400 KB, which holds
    roughly a thousand expenses; past that the put fails, and the caller deletes nothing.

    Args:
        pk: Partition key of the ledger whose trip is ending (e.g. 'USER#123456789').
        start_date: The trip's start date, from its TRIP#ACTIVE marker.
        expenses: Every expense item of the trip, as returned by list_expenses.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails, including when
            the item exceeds DynamoDB's size limit.
    """
    ended_at = datetime.now(timezone.utc)
    expires_at = ended_at + timedelta(seconds=settings.TRIP_ARCHIVE_TTL_SECONDS)
    dynamodb.put_item(
        {
            "PK": pk,
            "SK": f"{ARCHIVE_SK_PREFIX}{ended_at.isoformat(timespec='microseconds')}",
            "start_date": start_date,
            "ended_at": ended_at.isoformat(timespec="microseconds"),
            # PK is the same for every expense and already on the archive item.
            "expenses": [
                {key: value for key, value in expense.items() if key != "PK"}
                for expense in expenses
            ],
            dynamodb.TTL_ATTRIBUTE: int(expires_at.timestamp()),
        }
    )


@tool
def end_trip(
    ledger_id: Annotated[str, InjectedState("ledger_id")],
) -> str:
    """End the active trip, export its expenses as CSV, and archive then delete them.

    Call this when the user asks to end the trip. Always call get_all_expenses first to
    present the summary, then call this tool. The user will be shown a confirmation
    prompt by the application before this tool actually executes.

    The CSV is built and the expenses archived before anything is deleted, so a failure
    at either step leaves the trip intact rather than destroying records with no copy of
    them. The archive expires after TRIP_ARCHIVE_TTL_SECONDS.

    Args:
        ledger_id: The Telegram user ID of the user ending the trip.

    Returns:
        A confirmation line followed by a CSV of every expense in the trip, including an
        amount_sgd column, so the summary can be written from real figures. If exchange
        rates were unavailable, amount_sgd is blank and the CSV is prefixed with an
        instruction not to report SGD figures. Returns an error string if no trip is
        active.

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB request fails.
    """
    pk = f"USER#{ledger_id}"
    active_trip = dynamodb.get_item(pk, "TRIP#ACTIVE")
    if active_trip is None:
        return NO_ACTIVE_TRIP

    # Sorted by date, so the CSV the summary is written from reads chronologically.
    expenses = list_expenses(ledger_id)

    fx_rates: dict[str, float] = {}
    rates_unavailable = False
    if expenses:
        try:
            fx_rates = get_sgd_exchange_rates()
        except (httpx.HTTPError, RuntimeError, ValidationError):
            logger.exception(
                "FX rate fetch failed for user %s; ending trip without SGD conversion",
                ledger_id,
            )
            rates_unavailable = True

    # Export and archive before deleting. If either raises, the tool fails and nothing is
    # removed. A trip with no expenses has nothing worth keeping, so it is not archived.
    csv_text = generate_csv(expenses, fx_rates).decode("utf-8")
    if expenses:
        _archive_trip(pk, active_trip["start_date"], expenses)

    for expense in expenses:
        dynamodb.delete_item(pk, expense["SK"])
    dynamodb.delete_item(pk, "TRIP#ACTIVE")

    if rates_unavailable:
        return f"{END_TRIP_SUCCESS}\n\n{FX_UNAVAILABLE_NOTICE}\n\n{csv_text}"
    return f"{END_TRIP_SUCCESS}\n\n{csv_text}"
