"""LangChain tools for starting and ending an overseas trip."""

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from src.bot.storage import dynamodb


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
    """End the current active trip and delete all associated expense records.

    Call this when the user asks to end the trip. Always call get_all_expenses
    first to present the summary, then call this tool. The user will be shown a
    confirmation prompt by the application before this tool actually executes.

    Args:
        telegram_user_id: The Telegram user ID of the user ending the trip.

    Returns:
        A confirmation string on success, or an error string if no trip is active.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    pk = f"USER#{telegram_user_id}"
    if dynamodb.get_item(pk, "TRIP#ACTIVE") is None:
        return "There are no active trips to be ended. Start a new trip and add expenses first."

    expenses = dynamodb.query_by_prefix(pk, "EXPENSE#")
    for expense in expenses:
        dynamodb.delete_item(pk, expense["SK"])

    dynamodb.delete_item(pk, "TRIP#ACTIVE")

    return "Trip successfully ended."
