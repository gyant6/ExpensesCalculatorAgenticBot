from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import boto3.session
import pytest
from botocore.exceptions import ClientError

from src.bot.config import settings
from src.bot.storage import dynamodb

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient


class FakePaginator:
    """Yields pre-canned query pages, standing in for boto3's real paginator."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.call_kwargs: dict[str, Any] = {}

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.call_kwargs = kwargs
        return self.pages


class FakeClient:
    """Minimal stand-in for a boto3 DynamoDB client exposing only get_paginator."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.paginator = FakePaginator(pages)
        self.requested: str | None = None

    def get_paginator(self, operation_name: str) -> FakePaginator:
        self.requested = operation_name
        return self.paginator


def test_update_item_updates_existing_item(dynamodb_table: DynamoDBClient) -> None:
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1",
    }

    dynamodb.put_item(item)

    update = {"amount": "1.50"}
    dynamodb.update_item(item["PK"], item["SK"], update)
    updated_item = dynamodb.get_item(item["PK"], item["SK"])

    assert item | update == updated_item


def test_update_item_raises_on_nonexistent_item(dynamodb_table: DynamoDBClient) -> None:
    with pytest.raises(ClientError) as exc_info:
        dynamodb.update_item("unknown", "unknown", {"unknown": "unknown"})

    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


def test_update_item_raises_on_empty_fields() -> None:
    with pytest.raises(ValueError):
        dynamodb.update_item("unknown", "unknown", {})


def test_put_item(dynamodb_table: DynamoDBClient) -> None:
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1",
    }

    dynamodb.put_item(item)

    assert item == dynamodb.get_item(item["PK"], item["SK"])


def test_get_item_returns_none_when_not_found(dynamodb_table: DynamoDBClient) -> None:
    item = dynamodb.get_item("Unknown", "Unknown")
    assert item is None


def test_get_item_returns_item_when_found(dynamodb_table: DynamoDBClient) -> None:
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1",
    }
    dynamodb.put_item(item)
    assert item == dynamodb.get_item(item["PK"], item["SK"])


def test_delete_existing_item(dynamodb_table: DynamoDBClient) -> None:
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1",
    }
    dynamodb.put_item(item)
    dynamodb.delete_item(item["PK"], item["SK"])
    assert dynamodb.get_item(item["PK"], item["SK"]) is None


def test_delete_nonexistent_item(dynamodb_table: DynamoDBClient) -> None:
    dynamodb.delete_item("Unknown", "Unknown")


# ── conditional update and delete ────────────────────────────────────────────

VERSIONED_ITEM: dict[str, Any] = {
    "PK": "USER#00000000",
    "SK": "EXPENSE#k7qm",
    "amount": "1.00",
    "updated_at": "2026-09-15T02:51:00.000000+00:00",
}
AS_READ = {"amount": "1.00", "updated_at": "2026-09-15T02:51:00.000000+00:00"}


def test_update_item_if_unchanged_applies_when_item_matches(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item(VERSIONED_ITEM)

    applied = dynamodb.update_item_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", {"amount": "1.50"}, AS_READ
    )

    assert applied is True
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") == {
        **VERSIONED_ITEM,
        "amount": "1.50",
    }


def test_update_item_if_unchanged_refuses_a_changed_item(
    dynamodb_table: DynamoDBClient,
) -> None:
    changed = {**VERSIONED_ITEM, "updated_at": "2026-09-15T03:00:00.000000+00:00"}
    dynamodb.put_item(changed)

    applied = dynamodb.update_item_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", {"amount": "1.50"}, AS_READ
    )

    assert applied is False
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") == changed


def test_update_item_if_unchanged_refuses_a_missing_item_without_creating_it(
    dynamodb_table: DynamoDBClient,
) -> None:
    applied = dynamodb.update_item_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", {"amount": "1.50"}, AS_READ
    )

    assert applied is False
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") is None


def test_update_item_if_unchanged_treats_none_as_absent(
    dynamodb_table: DynamoDBClient,
) -> None:
    legacy = {key: v for key, v in VERSIONED_ITEM.items() if key != "updated_at"}
    dynamodb.put_item(legacy)

    applied = dynamodb.update_item_if_unchanged(
        "USER#00000000",
        "EXPENSE#k7qm",
        {"updated_at": "2026-09-15T03:00:00.000000+00:00"},
        {"amount": "1.00", "updated_at": None},
    )

    assert applied is True


@pytest.mark.parametrize(
    ("fields", "expected"), [({}, AS_READ), ({"amount": "1.50"}, {})]
)
def test_update_item_if_unchanged_rejects_empty_arguments(
    fields: dict[str, Any], expected: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        dynamodb.update_item_if_unchanged("unknown", "unknown", fields, expected)


# ── guarded writes ───────────────────────────────────────────────────────────

GUARD = dynamodb.Guard(
    pk="USER#00000000", sk="TRIP#ACTIVE", attribute="status", forbidden_value="ENDING"
)
APPLIED = dynamodb.WriteOutcome.APPLIED
ITEM_CONDITION_FAILED = dynamodb.WriteOutcome.ITEM_CONDITION_FAILED
GUARD_FAILED = dynamodb.WriteOutcome.GUARD_FAILED


def _set_guard_status(status: str) -> None:
    dynamodb.put_item({"PK": "USER#00000000", "SK": "TRIP#ACTIVE", "status": status})


def test_guarded_put_writes_when_the_guard_item_is_missing(
    dynamodb_table: DynamoDBClient,
) -> None:
    assert dynamodb.guarded_put_if_absent(VERSIONED_ITEM, GUARD) is APPLIED
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") == VERSIONED_ITEM


def test_guarded_put_writes_when_the_guard_allows_it(
    dynamodb_table: DynamoDBClient,
) -> None:
    _set_guard_status("ACTIVE")

    assert dynamodb.guarded_put_if_absent(VERSIONED_ITEM, GUARD) is APPLIED


def test_guarded_put_is_refused_by_the_guard(dynamodb_table: DynamoDBClient) -> None:
    _set_guard_status("ENDING")

    assert dynamodb.guarded_put_if_absent(VERSIONED_ITEM, GUARD) is GUARD_FAILED
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") is None


def test_guarded_put_reports_a_taken_key(dynamodb_table: DynamoDBClient) -> None:
    dynamodb.put_item(VERSIONED_ITEM)

    outcome = dynamodb.guarded_put_if_absent({**VERSIONED_ITEM, "amount": "9"}, GUARD)

    assert outcome is ITEM_CONDITION_FAILED
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") == VERSIONED_ITEM


def test_guarded_update_applies_when_unchanged_and_allowed(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item(VERSIONED_ITEM)

    outcome = dynamodb.guarded_update_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", {"amount": "1.50"}, AS_READ, GUARD
    )

    assert outcome is APPLIED
    item = dynamodb.get_item("USER#00000000", "EXPENSE#k7qm")
    assert item is not None
    assert item["amount"] == "1.50"


def test_guarded_update_reports_a_changed_item(dynamodb_table: DynamoDBClient) -> None:
    changed = {**VERSIONED_ITEM, "amount": "2.00"}
    dynamodb.put_item(changed)

    outcome = dynamodb.guarded_update_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", {"amount": "1.50"}, AS_READ, GUARD
    )

    assert outcome is ITEM_CONDITION_FAILED
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") == changed


def test_guarded_update_is_refused_by_the_guard(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item(VERSIONED_ITEM)
    _set_guard_status("ENDING")

    outcome = dynamodb.guarded_update_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", {"amount": "1.50"}, AS_READ, GUARD
    )

    assert outcome is GUARD_FAILED
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") == VERSIONED_ITEM


def test_guarded_delete_deletes_when_unchanged_and_allowed(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item(VERSIONED_ITEM)

    outcome = dynamodb.guarded_delete_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", AS_READ, GUARD
    )

    assert outcome is APPLIED
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") is None


def test_guarded_delete_keeps_a_changed_item(dynamodb_table: DynamoDBClient) -> None:
    changed = {**VERSIONED_ITEM, "amount": "2.00"}
    dynamodb.put_item(changed)

    outcome = dynamodb.guarded_delete_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", AS_READ, GUARD
    )

    assert outcome is ITEM_CONDITION_FAILED
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") == changed


def test_guarded_delete_reports_a_missing_item(dynamodb_table: DynamoDBClient) -> None:
    outcome = dynamodb.guarded_delete_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", AS_READ, GUARD
    )

    assert outcome is ITEM_CONDITION_FAILED


def test_guarded_delete_is_refused_by_the_guard(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item(VERSIONED_ITEM)
    _set_guard_status("ENDING")

    outcome = dynamodb.guarded_delete_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", AS_READ, GUARD
    )

    assert outcome is GUARD_FAILED
    assert dynamodb.get_item("USER#00000000", "EXPENSE#k7qm") == VERSIONED_ITEM


def test_guarded_delete_accepts_only_absent_expectations(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item({"PK": "USER#00000000", "SK": "EXPENSE#k7qm"})

    outcome = dynamodb.guarded_delete_if_unchanged(
        "USER#00000000", "EXPENSE#k7qm", {"updated_at": None}, GUARD
    )

    assert outcome is APPLIED


def test_guarded_delete_rejects_empty_expectations() -> None:
    with pytest.raises(ValueError):
        dynamodb.guarded_delete_if_unchanged("unknown", "unknown", {}, GUARD)


# ── transaction conflicts ────────────────────────────────────────────────────


def _client_error(code: str, reasons: list[str] | None = None) -> ClientError:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": code}}
    if reasons is not None:
        response["CancellationReasons"] = [{"Code": reason} for reason in reasons]
    return ClientError(response, "operation")  # type: ignore[arg-type]


class FlakyWriter:
    """A client method that raises the given errors once each, then succeeds."""

    def __init__(self, *errors: ClientError) -> None:
        self.errors = list(errors)
        self.calls = 0

    def __call__(self, **request: Any) -> dict[str, Any]:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {}


class FakeWriteClient:
    """Stands in for the DynamoDB client, routing writes to FlakyWriter instances."""

    def __init__(self, writer: FlakyWriter) -> None:
        self.put_item = writer
        self.transact_write_items = writer


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.bot.storage.dynamodb.time.sleep", lambda _seconds: None)


def test_a_write_retries_a_transaction_conflict(
    monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    writer = FlakyWriter(_client_error("TransactionConflictException"))
    monkeypatch.setattr(dynamodb, "get_client", lambda: FakeWriteClient(writer))

    dynamodb.put_item(VERSIONED_ITEM)

    assert writer.calls == 2


def test_a_write_gives_up_on_a_persistent_conflict(
    monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    conflict = _client_error("TransactionConflictException")
    writer = FlakyWriter(*[conflict] * 4)
    monkeypatch.setattr(dynamodb, "get_client", lambda: FakeWriteClient(writer))

    with pytest.raises(ClientError):
        dynamodb.put_item(VERSIONED_ITEM)
    assert writer.calls == 4


def test_a_write_does_not_retry_other_errors(
    monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    writer = FlakyWriter(_client_error("ValidationException"))
    monkeypatch.setattr(dynamodb, "get_client", lambda: FakeWriteClient(writer))

    with pytest.raises(ClientError):
        dynamodb.put_item(VERSIONED_ITEM)
    assert writer.calls == 1


def test_a_guarded_write_retries_a_cancelled_conflict(
    monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    writer = FlakyWriter(
        _client_error("TransactionCanceledException", ["None", "TransactionConflict"])
    )
    monkeypatch.setattr(dynamodb, "get_client", lambda: FakeWriteClient(writer))

    assert dynamodb.guarded_put_if_absent(VERSIONED_ITEM, GUARD) is APPLIED
    assert writer.calls == 2


def test_a_guarded_write_gives_up_on_a_persistent_conflict(
    monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    conflict = _client_error(
        "TransactionCanceledException", ["TransactionConflict", "None"]
    )
    writer = FlakyWriter(*[conflict] * 4)
    monkeypatch.setattr(dynamodb, "get_client", lambda: FakeWriteClient(writer))

    with pytest.raises(ClientError):
        dynamodb.guarded_put_if_absent(VERSIONED_ITEM, GUARD)
    assert writer.calls == 4


def test_a_guarded_write_raises_a_cancellation_without_a_known_reason(
    monkeypatch: pytest.MonkeyPatch, no_backoff: None
) -> None:
    writer = FlakyWriter(
        _client_error("TransactionCanceledException", ["None", "ValidationError"])
    )
    monkeypatch.setattr(dynamodb, "get_client", lambda: FakeWriteClient(writer))

    with pytest.raises(ClientError):
        dynamodb.guarded_put_if_absent(VERSIONED_ITEM, GUARD)
    assert writer.calls == 1


def test_query_by_prefix_returns_empty_list(dynamodb_table: DynamoDBClient) -> None:
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1",
    }

    assert dynamodb.query_by_prefix(item["PK"], "EXPENSE#") == []


def test_query_by_prefix_returns_single_item(dynamodb_table: DynamoDBClient) -> None:
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1",
    }
    dynamodb.put_item(item)

    assert dynamodb.query_by_prefix(item["PK"], "EXPENSE#") == [item]


def test_query_by_prefix_returns_multiple_items(dynamodb_table: DynamoDBClient) -> None:
    item1 = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1",
    }
    item2 = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#2",
        "summary": "Dinner at Sushi Tei",
        "category": "Food",
        "amount": "2.00",
        "currency": "SGD",
        "date": "2020-12-30",
        "payment_method": "Card",
        "source_message": "Lunch at Sushi Tei $2",
    }
    dynamodb.put_item(item1)
    dynamodb.put_item(item2)
    result = dynamodb.query_by_prefix("USER#00000000", "EXPENSE#")

    assert sorted(result, key=lambda x: x["SK"]) == sorted(
        [item1, item2], key=lambda x: x["SK"]
    )


def test_query_by_prefix_returns_only_matching_items(
    dynamodb_table: DynamoDBClient,
) -> None:
    expense = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1",
    }
    trip = {"PK": "USER#00000000", "SK": "TRIP#1"}
    dynamodb.put_item(expense)
    dynamodb.put_item(trip)

    assert dynamodb.query_by_prefix("USER#00000000", "EXPENSE#") == [expense]


def test_query_by_prefix_returns_all_items_across_the_1mb_page_boundary(
    dynamodb_table: DynamoDBClient,
) -> None:
    """A partition larger than DynamoDB's 1 MB response cap must come back whole.

    The cap is on bytes, not rows, so ~120 items with a padded source_message cross it
    while keeping the test fast — padding that field is realistic, since edit_expense
    appends to it on every edit. moto enforces the same limit as real DynamoDB: before
    pagination a single query returned only 83 of these 120 items, which is how end_trip
    could delete part of a trip and still remove TRIP#ACTIVE.
    """
    padding = "x" * 12_000
    total = 120

    for start in range(0, total, 25):
        dynamodb_table.batch_write_item(
            RequestItems={
                settings.DYNAMODB_TABLE_NAME: [
                    {
                        "PutRequest": {
                            "Item": {
                                "PK": {"S": "USER#paged"},
                                "SK": {"S": f"EXPENSE#{i:04d}"},
                                "source_message": {"S": padding},
                            }
                        }
                    }
                    for i in range(start, min(start + 25, total))
                ]
            }
        )

    result = dynamodb.query_by_prefix("USER#paged", "EXPENSE#")

    assert len(result) == total
    assert [item["SK"] for item in result] == [f"EXPENSE#{i:04d}" for i in range(total)]


def test_query_by_prefix_tolerates_a_page_without_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DynamoDB can return a page whose filter matched nothing while more pages remain."""
    fake = FakeClient(
        [
            {"Items": [{"PK": {"S": "USER#1"}, "SK": {"S": "EXPENSE#1"}}]},
            {},
            {"Items": [{"PK": {"S": "USER#1"}, "SK": {"S": "EXPENSE#2"}}]},
        ]
    )
    monkeypatch.setattr(dynamodb, "get_client", lambda: fake)

    result = dynamodb.query_by_prefix("USER#1", "EXPENSE#")

    assert [item["SK"] for item in result] == ["EXPENSE#1", "EXPENSE#2"]


# ── shared client ────────────────────────────────────────────────────────────


def test_get_client_reuses_one_client() -> None:
    assert dynamodb.get_client() is dynamodb.get_client()


def test_get_client_builds_a_new_client_after_reset() -> None:
    first = dynamodb.get_client()

    dynamodb.reset_clients()

    assert dynamodb.get_client() is not first


def test_get_client_keeps_a_client_per_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "DYNAMODB_ENDPOINT_URL", None)
    aws = dynamodb.get_client()
    monkeypatch.setattr(settings, "DYNAMODB_ENDPOINT_URL", "http://127.0.0.1:8000")

    local = dynamodb.get_client()

    assert local is not aws
    assert local.meta.endpoint_url == "http://127.0.0.1:8000"


def test_simultaneous_first_calls_create_exactly_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel tool calls must not build clients concurrently, nor end up with two."""
    threads = 8
    barrier = threading.Barrier(threads)
    real_session = boto3.session.Session
    sessions_created = 0
    count_lock = threading.Lock()

    def counting_session(*args: Any, **kwargs: Any) -> boto3.session.Session:
        nonlocal sessions_created
        with count_lock:
            sessions_created += 1
        return real_session(*args, **kwargs)

    monkeypatch.setattr(boto3.session, "Session", counting_session)

    def first_call() -> object:
        barrier.wait(timeout=10)
        return dynamodb.get_client()

    with ThreadPoolExecutor(max_workers=threads) as pool:
        clients = list(pool.map(lambda _: first_call(), range(threads)))

    assert sessions_created == 1
    assert all(client is clients[0] for client in clients)
