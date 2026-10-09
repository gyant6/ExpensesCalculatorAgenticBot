from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import respx
from botocore.exceptions import ClientError
from httpx import Response

from src.bot.config import settings
from src.bot.export import CSV_FIELDNAMES
from src.bot.storage import dynamodb
from src.bot.tools import trip
from src.bot.tools.fx import FX_URL

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

TELEGRAM_USER_ID = "123456"
CSV_HEADER = ",".join(CSV_FIELDNAMES)


# 02:51 UTC on 15 Sep is still the evening of 14 Sep in Los Angeles (UTC−7).
MESSAGE_TIME = "2026-09-15T02:51:00+00:00"


def _start(timezone: str) -> str:
    return str(
        trip.start_trip.invoke(
            {
                "ledger_id": TELEGRAM_USER_ID,
                "message_time": MESSAGE_TIME,
                "timezone": timezone,
            }
        )
    )


def _set_zone(timezone: str) -> str:
    return str(
        trip.set_trip_timezone.invoke(
            {
                "ledger_id": TELEGRAM_USER_ID,
                "message_time": MESSAGE_TIME,
                "timezone": timezone,
            }
        )
    )


def test_start_trip_records_the_local_start_date_and_zone(
    dynamodb_table: DynamoDBClient,
) -> None:
    tool_output = _start("America/Los_Angeles")

    pk = f"USER#{TELEGRAM_USER_ID}"
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") == {
        "PK": pk,
        "SK": "TRIP#ACTIVE",
        "start_date": "2026-09-14",
        "timezone": "America/Los_Angeles",
    }
    assert tool_output == "New trip started on 2026-09-14 (America/Los_Angeles)."


def test_start_trip_rejects_an_unknown_time_zone(
    dynamodb_table: DynamoDBClient,
) -> None:
    assert "not a valid IANA time zone" in _start("Hawai/Somewhere")
    assert dynamodb.get_item(f"USER#{TELEGRAM_USER_ID}", "TRIP#ACTIVE") is None


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

    assert _start("Asia/Tokyo") == "There is already an active trip."


def test_set_trip_timezone_moves_the_active_trip(
    dynamodb_table: DynamoDBClient,
) -> None:
    _start("America/Los_Angeles")

    tool_output = _set_zone("Asia/Seoul")

    record = dynamodb.get_item(f"USER#{TELEGRAM_USER_ID}", "TRIP#ACTIVE")
    assert record is not None
    assert record["timezone"] == "Asia/Seoul"
    assert record["start_date"] == "2026-09-14"
    # 02:51 UTC on 15 Sep is 11:51 on 15 Sep in Seoul.
    assert tool_output == (
        "Time zone set to Asia/Seoul. Today there is Tuesday, 15 September 2026."
    )


def test_set_trip_timezone_without_a_trip_is_refused(
    dynamodb_table: DynamoDBClient,
) -> None:
    assert _set_zone("Asia/Seoul") == "There is no active trip. Start a trip first."
    assert dynamodb.get_item(f"USER#{TELEGRAM_USER_ID}", "TRIP#ACTIVE") is None


def test_set_trip_timezone_rejects_an_unknown_time_zone(
    dynamodb_table: DynamoDBClient,
) -> None:
    _start("America/Los_Angeles")

    assert "not a valid IANA time zone" in _set_zone("Mars/Olympus")
    record = dynamodb.get_item(f"USER#{TELEGRAM_USER_ID}", "TRIP#ACTIVE")
    assert record is not None
    assert record["timezone"] == "America/Los_Angeles"


def test_end_trip_with_no_expenses(dynamodb_table: DynamoDBClient) -> None:
    """A trip with no expenses still ends, returning a CSV containing only its header.

    No FX call is made, so no respx mock is needed — an unmocked request would fail.
    """
    pk = f"USER#{TELEGRAM_USER_ID}"
    dynamodb.put_item({"PK": pk, "SK": "TRIP#ACTIVE", "start_date": "2025-12-20"})

    tool_output = trip.end_trip.invoke({"ledger_id": TELEGRAM_USER_ID})
    assert tool_output.startswith(trip.END_TRIP_SUCCESS)
    assert CSV_HEADER in tool_output
    assert trip.FX_UNAVAILABLE_NOTICE not in tool_output
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") is None
    assert dynamodb.query_by_prefix(pk, trip.ARCHIVE_SK_PREFIX) == []


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

    tool_output = trip.end_trip.invoke({"ledger_id": TELEGRAM_USER_ID})

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

    tool_output = trip.end_trip.invoke({"ledger_id": TELEGRAM_USER_ID})

    assert trip.FX_UNAVAILABLE_NOTICE in tool_output
    assert CSV_HEADER in tool_output
    assert "Breakfast at Yakun" in tool_output
    assert dynamodb.query_by_prefix(pk, "EXPENSE#") == []
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") is None


@respx.mock
def test_end_trip_archives_every_expense_with_an_expiry(
    dynamodb_table: DynamoDBClient, base_expense: dict[str, Any]
) -> None:
    """The archive is the only copy besides the CSV in the chat, so it must hold every
    expense in full, and carry the expiry DynamoDB's TTL process deletes it by.
    """
    respx.get(FX_URL).mock(
        return_value=Response(200, json={"success": True, "rates": {"JPY": 124.1}})
    )
    pk = f"USER#{TELEGRAM_USER_ID}"
    dynamodb.put_item({"PK": pk, "SK": "TRIP#ACTIVE", "start_date": "2025-12-20"})
    dynamodb.put_item({"PK": pk, "SK": "EXPENSE#1", **base_expense})
    dynamodb.put_item({"PK": pk, "SK": "EXPENSE#2", **base_expense, "amount": "9.5"})

    before = int(datetime.now(timezone.utc).timestamp())
    trip.end_trip.invoke({"ledger_id": TELEGRAM_USER_ID})
    after = int(datetime.now(timezone.utc).timestamp())

    archives = dynamodb.query_by_prefix(pk, trip.ARCHIVE_SK_PREFIX)
    assert len(archives) == 1
    archive = archives[0]
    assert archive["start_date"] == "2025-12-20"
    assert archive["SK"] == f"{trip.ARCHIVE_SK_PREFIX}{archive['ended_at']}"
    assert archive["expenses"] == [
        {"SK": "EXPENSE#1", **base_expense},
        {"SK": "EXPENSE#2", **base_expense, "amount": "9.5"},
    ]
    retention = settings.TRIP_ARCHIVE_TTL_SECONDS
    assert before + retention <= archive[dynamodb.TTL_ATTRIBUTE] <= after + retention


@respx.mock
def test_archive_is_invisible_to_the_next_trip(
    dynamodb_table: DynamoDBClient, base_expense: dict[str, Any]
) -> None:
    # The archive shares the ledger's partition with live expenses, so it must sit
    # outside the EXPENSE# prefix every expense tool lists, edits and deletes by.
    respx.get(FX_URL).mock(
        return_value=Response(200, json={"success": True, "rates": {"JPY": 124.1}})
    )
    pk = f"USER#{TELEGRAM_USER_ID}"
    dynamodb.put_item({"PK": pk, "SK": "TRIP#ACTIVE", "start_date": "2025-12-20"})
    dynamodb.put_item({"PK": pk, "SK": "EXPENSE#1", **base_expense})
    trip.end_trip.invoke({"ledger_id": TELEGRAM_USER_ID})

    _start("Asia/Seoul")

    assert dynamodb.query_by_prefix(pk, "EXPENSE#") == []
    assert len(dynamodb.query_by_prefix(pk, trip.ARCHIVE_SK_PREFIX)) == 1


@respx.mock
def test_nothing_is_deleted_when_the_archive_cannot_be_written(
    dynamodb_table: DynamoDBClient, base_expense: dict[str, Any]
) -> None:
    respx.get(FX_URL).mock(
        return_value=Response(200, json={"success": True, "rates": {"JPY": 124.1}})
    )
    pk = f"USER#{TELEGRAM_USER_ID}"
    dynamodb.put_item({"PK": pk, "SK": "TRIP#ACTIVE", "start_date": "2025-12-20"})
    dynamodb.put_item({"PK": pk, "SK": "EXPENSE#1", **base_expense})
    rejected = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "Item size too large"}},
        "PutItem",
    )

    with patch.object(dynamodb, "put_item", side_effect=rejected):
        with pytest.raises(ClientError, match="Item size too large"):
            trip.end_trip.invoke({"ledger_id": TELEGRAM_USER_ID})

    assert len(dynamodb.query_by_prefix(pk, "EXPENSE#")) == 1
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") is not None


def test_end_trip_returns_error_when_no_active_trip(
    dynamodb_table: DynamoDBClient,
) -> None:
    tool_output = trip.end_trip.invoke({"ledger_id": TELEGRAM_USER_ID})
    assert tool_output == trip.NO_ACTIVE_TRIP
