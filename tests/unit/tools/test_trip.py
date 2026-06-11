from src.bot.tools import trip
from src.bot.storage import dynamodb

from datetime import datetime
from unittest.mock import patch

TELEGRAM_USER_ID = "123456"


def test_start_trip_creates_active_trip(dynamodb_table):
    mock_datetime = datetime(2025, 12, 20)
    with patch("src.bot.tools.trip.datetime") as mock_dt:
        mock_dt.now.return_value = mock_datetime
        tool_output = trip.start_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})

    pk = f"USER#{TELEGRAM_USER_ID}"
    sk = "TRIP#ACTIVE"
    mock_datetime_str = mock_datetime.strftime("%Y-%m-%d")
    record = dynamodb.get_item(pk, sk)
    assert record == {"PK": pk, "SK": sk, "start_date": mock_datetime_str}
    assert tool_output == f"New trip started on {mock_datetime_str}."


def test_start_trip_returns_error_when_trip_already_active(dynamodb_table):
    dynamodb.put_item(
        {
            "PK": f"USER#{TELEGRAM_USER_ID}",
            "SK": "TRIP#ACTIVE",
            "start_date": "2020-12-30",
        }
    )

    tool_output = trip.start_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})
    assert tool_output == "There is already an active trip."


def test_end_trip_with_no_expenses(dynamodb_table):
    pk = f"USER#{TELEGRAM_USER_ID}"
    dynamodb.put_item({"PK": pk, "SK": "TRIP#ACTIVE", "start_date": "2025-12-20"})

    tool_output = trip.end_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})
    assert tool_output == "Trip successfully ended."
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") is None


def test_end_trip_deletes_all_expenses(dynamodb_table, base_expense):
    pk = f"USER#{TELEGRAM_USER_ID}"
    dynamodb.put_item({"PK": pk, "SK": "TRIP#ACTIVE", "start_date": "2025-12-20"})

    dynamodb.put_item({"PK": pk, "SK": "EXPENSE#1", **base_expense})

    dynamodb.put_item({"PK": pk, "SK": "EXPENSE#2", **base_expense})

    tool_output = trip.end_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})
    assert tool_output == "Trip successfully ended."
    assert dynamodb.query_by_prefix(pk, "EXPENSE#") == []
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") is None


def test_end_trip_returns_error_when_no_active_trip(dynamodb_table):
    tool_output = trip.end_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})
    assert (
        tool_output
        == "There are no active trips to be ended. Start a new trip and add expenses first."
    )
