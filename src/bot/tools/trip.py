from src.bot.storage import dynamodb

from datetime import datetime
from langchain_core.tools import tool


@tool
def start_trip(telegram_user_id: str) -> str:
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
    
    start_date = datetime.today().strftime('%Y-%m-%d')
    dynamodb.put_item({
        "PK": f"USER#{telegram_user_id}",
        "SK": "TRIP#ACTIVE",
        "start_date": start_date
    })
    
    return f"New trip started on {start_date}."

