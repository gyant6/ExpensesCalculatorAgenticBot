"""Tests for turning a message's UTC time into the trip's local day.

Before trips carried a time zone, the default expense date was the UTC day: the first
expense of the September trip, logged at 06:57 SGT on 12 Sep, was recorded as 11 Sep —
a day before the trip began — and anywhere behind UTC lost the evening to the next day.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from src.bot.agent.nodes import check_trip_status
from src.bot.agent.state import AgentState
from src.bot.storage import dynamodb
from src.bot.timezones import (
    DEFAULT_TIMEZONE,
    describe_date,
    is_valid_timezone,
    local_date,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

LEDGER = "123456"


# ── local_date ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message_time", "timezone", "expected"),
    [
        # Behind UTC: 02:51 UTC on 15 Sep is the evening of 14 Sep in Los Angeles.
        ("2026-09-15T02:51:00+00:00", "America/Los_Angeles", "2026-09-14"),
        # Ahead of UTC: 22:57 UTC on 11 Sep is the morning of 12 Sep in Singapore — the
        # September trip's first expense, which the UTC default recorded as the 11th.
        ("2026-09-11T22:57:00+00:00", "Asia/Singapore", "2026-09-12"),
        # The same moment in UTC itself, for contrast.
        ("2026-09-11T22:57:00+00:00", "UTC", "2026-09-11"),
    ],
)
def test_local_date_is_the_day_in_the_given_zone(
    message_time: str, timezone: str, expected: str
) -> None:
    assert local_date(message_time, timezone) == expected


def test_local_date_refuses_a_time_without_an_offset() -> None:
    with pytest.raises(ValueError, match="no UTC offset"):
        local_date("2026-09-15T02:51:00", "Asia/Tokyo")


def test_describe_date_includes_the_weekday() -> None:
    assert describe_date("2026-09-14") == "Monday, 14 September 2026"
    assert describe_date("2026-10-01") == "Thursday, 1 October 2026"


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("Asia/Tokyo", True),
        ("America/Los_Angeles", True),
        (DEFAULT_TIMEZONE, True),
        ("Hawai/Somewhere", False),
        ("../etc/passwd", False),
        ("", False),
    ],
)
def test_is_valid_timezone(name: str, valid: bool) -> None:
    assert is_valid_timezone(name) is valid


# ── check_trip_status ────────────────────────────────────────────────────────


def _state(message_time: str) -> AgentState:
    return AgentState(messages=[], ledger_id=LEDGER, message_time=message_time)


def test_without_a_trip_the_day_is_in_the_default_zone(
    dynamodb_table: DynamoDBClient,
) -> None:
    assert check_trip_status(_state("2026-09-11T22:57:00+00:00")) == {
        "trip_start_date": None,
        "trip_timezone": DEFAULT_TIMEZONE,
        "local_date": "2026-09-12",
    }


def test_with_a_trip_the_day_is_in_the_trip_zone(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item(
        {
            "PK": f"USER#{LEDGER}",
            "SK": "TRIP#ACTIVE",
            "start_date": "2026-09-12",
            "timezone": "America/Los_Angeles",
        }
    )

    assert check_trip_status(_state("2026-09-15T02:51:00+00:00")) == {
        "trip_start_date": "2026-09-12",
        "trip_timezone": "America/Los_Angeles",
        "local_date": "2026-09-14",
    }


def test_a_trip_without_a_zone_falls_back_to_the_default(
    dynamodb_table: DynamoDBClient,
) -> None:
    # A trip started before trips carried a zone.
    dynamodb.put_item(
        {"PK": f"USER#{LEDGER}", "SK": "TRIP#ACTIVE", "start_date": "2026-09-12"}
    )

    result = check_trip_status(_state("2026-09-11T22:57:00+00:00"))

    assert result["trip_timezone"] == DEFAULT_TIMEZONE
    assert result["local_date"] == "2026-09-12"


# ── graph wiring ─────────────────────────────────────────────────────────────


def test_tools_return_through_check_trip_status(
    dynamodb_table: DynamoDBClient,
) -> None:
    # So "start trip in Seoul, coffee 5" dates the coffee in Seoul: the trip started by
    # the first tool call is re-read before the model records the expense.
    from src.bot.agent.graph import build_graph

    edges = {(edge.source, edge.target) for edge in build_graph().get_graph().edges}

    assert ("tools_node", "check_trip_status") in edges
    assert ("tools_node", "agent_node") not in edges
