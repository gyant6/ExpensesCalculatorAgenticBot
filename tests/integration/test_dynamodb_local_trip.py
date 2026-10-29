"""Exercise an artificial trip through the real graph and DynamoDB Local."""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Callable, Generator
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from mypy_boto3_dynamodb import DynamoDBClient

from src.bot.agent.graph import build_graph, clear_thread_history, thread_id_for
from src.bot.config import settings
from src.bot.conversation import (
    awaiting_confirmation,
    config_for,
    confirm_end_trip,
    render_confirmed_trip,
    send_message,
)
from src.bot.storage import dynamodb
from src.bot.tools import trip
from src.bot.tools.expenses import list_expenses

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DYNAMODB_LOCAL_TESTS") != "1",
    reason="Set RUN_DYNAMODB_LOCAL_TESTS=1 to run against DynamoDB Local",
)

MESSAGE_TIME = "2026-10-25T15:30:00+00:00"  # 26 October in Tokyo.
Reply = AIMessage | Callable[[list[BaseMessage]], AIMessage]


class ScriptedModel:
    """Supply tool decisions without Bedrock or any paid inference."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        """Consume the next scripted response, failing on unexpected model calls."""
        assert self.replies, "The graph made an unexpected model call"
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


def tool_call(name: str, **args: Any) -> AIMessage:
    """Build a provider-compatible scripted tool call."""
    return AIMessage(
        "", tool_calls=[{"name": name, "args": args, "id": uuid4().hex[:9]}]
    )


def expense_id_from_listing(messages: list[BaseMessage], summary: str) -> str:
    """Read an expense ID from the latest real get_all_expenses tool response."""
    listing = messages[-1]
    assert isinstance(listing, ToolMessage) and listing.name == "get_all_expenses"
    for line in str(listing.content).splitlines()[1:]:
        fields = line.split(" | ")
        if fields[2] == summary:
            return fields[1]
    raise AssertionError(f"Expense {summary!r} missing from listing")


@pytest.fixture
def local_table(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Create and later remove only this test's uniquely named local table."""
    endpoint = os.environ.get("DYNAMODB_LOCAL_TEST_ENDPOINT", "http://127.0.0.1:8000")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        pytest.fail("DYNAMODB_LOCAL_TEST_ENDPOINT must be a local HTTP address")

    # The graph creates its own boto3 clients, so its entire credential chain must be
    # pinned to dummy values before construction. No AWS account is needed.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "local")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(settings, "AWS_REGION", "ap-southeast-1")
    monkeypatch.setattr(settings, "DYNAMODB_ENDPOINT_URL", endpoint)
    table_name = f"ExpensesBotIntegration-{uuid4().hex}"
    monkeypatch.setattr(settings, "DYNAMODB_TABLE_NAME", table_name)
    client: DynamoDBClient = boto3.client(
        "dynamodb",
        endpoint_url=endpoint,
        region_name=settings.AWS_REGION,
        aws_access_key_id="local",
        aws_secret_access_key="local",
        config=Config(
            connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1}
        ),
    )
    try:
        client.list_tables(Limit=1)
    except EndpointConnectionError as exc:
        pytest.fail(
            f"DynamoDB Local is unavailable at {endpoint}; start docker compose: {exc}"
        )

    created = False
    try:
        client.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        created = True
        client.get_waiter("table_exists").wait(TableName=table_name)
        yield
    finally:
        if created:
            client.delete_table(TableName=table_name)


def test_artificial_trip_lifecycle(
    local_table: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check real local writes, editing, listing, export, archive and cleanup."""
    monkeypatch.setattr(trip, "get_sgd_exchange_rates", lambda: {"JPY": 200.0})
    ledger_id = f"integration-{uuid4().hex}"

    def edit_lunch(messages: list[BaseMessage]) -> AIMessage:
        return tool_call(
            "edit_expense",
            expense_id=expense_id_from_listing(messages, "Lunch"),
            expected_amount=1200,
            edit_message="Lunch was 1400 yen",
            amount="1400",
        )

    def delete_museum(messages: list[BaseMessage]) -> AIMessage:
        return tool_call(
            "delete_expense",
            expense_id=expense_id_from_listing(messages, "Museum"),
            expected_amount=500,
        )

    model = ScriptedModel(
        tool_call("start_trip", timezone="Asia/Tokyo"),
        AIMessage("Trip started"),
        tool_call(
            "add_expense",
            source_message="Lunch yesterday 1200 yen",
            summary="Lunch",
            category="Food",
            amount="1200",
            currency="JPY",
            date="2026-10-25",
        ),
        AIMessage("Lunch recorded"),
        tool_call(
            "add_expense",
            source_message="Subway 700 yen",
            summary="Subway",
            category="Transport",
            amount="700",
            currency="JPY",
        ),
        AIMessage("Subway recorded"),
        tool_call(
            "add_expense",
            source_message="Coffee 8.50 SGD",
            summary="Coffee",
            category="Food",
            amount="8.50",
            currency="SGD",
        ),
        AIMessage("Coffee recorded"),
        tool_call(
            "add_expense",
            source_message="Museum 500 yen",
            summary="Museum",
            category="Leisure",
            amount="500",
            currency="JPY",
        ),
        AIMessage("Museum recorded"),
        tool_call("get_all_expenses"),
        edit_lunch,
        AIMessage("Lunch corrected"),
        tool_call("get_all_expenses"),
        delete_museum,
        AIMessage("Museum removed"),
        tool_call("get_all_expenses"),
        AIMessage("Here are the expenses"),
        tool_call("get_all_expenses"),
        tool_call("end_trip"),
    )
    graph = build_graph(model=model)

    def send(text: str) -> dict[str, Any]:
        return send_message(graph, ledger_id, text, MESSAGE_TIME)

    send("Start a trip in Tokyo")
    active = dynamodb.get_item(f"USER#{ledger_id}", "TRIP#ACTIVE")
    assert active is not None
    assert active["start_date"] == "2026-10-26"
    assert active["timezone"] == "Asia/Tokyo"

    send("Lunch yesterday 1200 yen")
    send("Subway 700 yen")
    send("Coffee 8.50 SGD")
    send("Museum 500 yen")
    assert len(list_expenses(ledger_id)) == 4
    send("Lunch was 1400 yen")
    send("Remove the museum")
    listed = send("Show all expenses")
    listing = next(
        message
        for message in reversed(listed["messages"])
        if isinstance(message, ToolMessage) and message.name == "get_all_expenses"
    )
    assert "Lunch" in str(listing.content)
    assert "Subway" in str(listing.content)
    assert "Coffee" in str(listing.content)
    assert "Museum" not in str(listing.content)
    expenses = list_expenses(ledger_id)
    assert [(item["summary"], item["amount"], item["date"]) for item in expenses] == [
        ("Lunch", Decimal("1400"), "2026-10-25"),
        ("Subway", Decimal("700"), "2026-10-26"),
        ("Coffee", Decimal("8.50"), "2026-10-26"),
    ]
    assert "Lunch was 1400 yen" in expenses[0]["source_message"]

    send("End the trip")
    assert awaiting_confirmation(graph.get_state(config_for(ledger_id)))
    assert dynamodb.get_item(f"USER#{ledger_id}", "TRIP#ACTIVE") is not None
    assert len(list_expenses(ledger_id)) == 3
    confirmation = confirm_end_trip(graph, ledger_id)
    assert confirmation.trip_ended
    summary, csv_bytes = render_confirmed_trip(confirmation)
    assert csv_bytes is not None
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
    assert [(row["summary"], row["amount_sgd"]) for row in rows] == [
        ("Lunch", "7.00"),
        ("Subway", "3.50"),
        ("Coffee", "8.50"),
    ]
    assert "Total: SGD 19.00" in summary
    assert "Food: SGD 15.50" in summary
    assert "Transport: SGD 3.50" in summary
    assert not model.replies

    pk = f"USER#{ledger_id}"
    assert dynamodb.get_item(pk, "TRIP#ACTIVE") is None
    assert list_expenses(ledger_id) == []
    archived = dynamodb.query_by_prefix(pk, trip.ARCHIVE_SK_PREFIX)
    assert len(archived) == 1
    assert {item["summary"] for item in archived[0]["expenses"]} == {
        "Lunch",
        "Subway",
        "Coffee",
    }
    clear_thread_history(graph, thread_id_for(ledger_id))
    assert not graph.get_state(config_for(ledger_id)).values
