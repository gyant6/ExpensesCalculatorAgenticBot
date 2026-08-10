from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import respx
from httpx import Response

from src.bot.charts import CSV_FIELDNAMES
from src.bot.storage import dynamodb
from src.bot.tools import trip
from src.bot.tools.fx import FX_URL

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

TELEGRAM_USER_ID = "123456"
CSV_HEADER = ",".join(CSV_FIELDNAMES)


def test_start_trip_creates_active_trip(dynamodb_table: DynamoDBClient) -> None:
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


def test_start_trip_returns_error_when_trip_already_active(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item(
        {
            "PK": f"USER#{TELEGRAM_USER_ID}",
            "SK": "TRIP#ACTIVE",
            "start_date": "2020-12-30",
        }
    )

    tool_output = trip.start_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})
    assert tool_output == "There is already an active trip."


def test_end_trip_with_no_expenses(dynamodb_table: DynamoDBClient) -> None:
    """A trip with no expenses still ends, returning a CSV containing only its header.

    No FX call is made, so no respx mock is needed — an unmocked request would fail.
    """
    pk = f"USER#{TELEGRAM_USER_ID}"
    dynamodb.put_item({"PK": pk, "SK": "TRIP#ACTIVE", "start_date": "2025-12-20"})

    tool_output = trip.end_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})
    assert tool_output.startswith(trip.END_TRIP_SUCCESS)
    assert CSV_HEADER in tool_output
    assert trip.FX_UNAVAILABLE_NOTICE not in tool_output
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") is None


@respx.mock
def test_end_trip_deletes_all_expenses_and_returns_csv(
    dynamodb_table: DynamoDBClient, base_expense: dict[str, str]
) -> None:
    respx.get(FX_URL).mock(
        return_value=Response(200, json={"success": True, "rates": {"JPY": 124.1}})
    )
    pk = f"USER#{TELEGRAM_USER_ID}"
    dynamodb.put_item({"PK": pk, "SK": "TRIP#ACTIVE", "start_date": "2025-12-20"})
    dynamodb.put_item({"PK": pk, "SK": "EXPENSE#1", **base_expense})
    dynamodb.put_item({"PK": pk, "SK": "EXPENSE#2", **base_expense})

    tool_output = trip.end_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})

    assert tool_output.startswith(trip.END_TRIP_SUCCESS)
    assert CSV_HEADER in tool_output
    # base_expense is in SGD, so amount_sgd equals the raw amount.
    assert tool_output.count("Breakfast at Yakun,Food,6.13,SGD,6.13") == 2
    assert dynamodb.query_by_prefix(pk, "EXPENSE#") == []
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") is None


@respx.mock
def test_end_trip_still_exports_and_deletes_when_rates_unavailable(
    dynamodb_table: DynamoDBClient, base_expense: dict[str, str]
) -> None:
    """A failed FX fetch must not block the trip ending or lose the expense export.

    The tool result carries an explicit instruction instead, because handing the model a
    bare success string makes it invent an exchange rate to produce an SGD total.
    """
    respx.get(FX_URL).mock(return_value=Response(500))
    pk = f"USER#{TELEGRAM_USER_ID}"
    dynamodb.put_item({"PK": pk, "SK": "TRIP#ACTIVE", "start_date": "2025-12-20"})
    dynamodb.put_item({"PK": pk, "SK": "EXPENSE#1", **base_expense})

    tool_output = trip.end_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})

    assert trip.FX_UNAVAILABLE_NOTICE in tool_output
    assert CSV_HEADER in tool_output
    assert "Breakfast at Yakun" in tool_output
    assert dynamodb.query_by_prefix(pk, "EXPENSE#") == []
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") is None


def test_end_trip_returns_error_when_no_active_trip(
    dynamodb_table: DynamoDBClient,
) -> None:
    tool_output = trip.end_trip.invoke({"telegram_user_id": TELEGRAM_USER_ID})
    assert tool_output == trip.NO_ACTIVE_TRIP
