"""Tests for recognising redelivered Telegram updates.

The HTTP API stops waiting on the Lambda after 30 seconds and Telegram then delivers the
update again. Observed in production on 29 Sep ("show all expenses" ran twice) and 7 Oct
(the trip end's retry was caught only by the confirmation claim). A redelivered
"add expense" would record the expense twice.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from src.bot.config import settings
from src.bot.dedup import UPDATE_MARKER_SK, UPDATE_PK_PREFIX, claim_update
from src.bot.main import _SECRET_TOKEN_HEADER, lambda_handler
from src.bot.storage import dynamodb

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

_SECRET = "s3cret-webhook-token"


@pytest.fixture
def processed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Authenticate deliveries and record each update that reaches processing."""
    monkeypatch.setattr(settings, "WEBHOOK_SECRET", _SECRET)
    seen: list[dict[str, Any]] = []

    async def _record(update_data: dict[str, Any]) -> None:
        seen.append(update_data)

    monkeypatch.setattr("src.bot.main._handle_update", _record)
    return seen


def _delivery(body: dict[str, Any]) -> dict[str, Any]:
    return {"headers": {_SECRET_TOKEN_HEADER: _SECRET}, "body": json.dumps(body)}


# ── put_item_if_absent ───────────────────────────────────────────────────────


def test_put_if_absent_writes_when_the_key_is_free(
    dynamodb_table: DynamoDBClient,
) -> None:
    assert dynamodb.put_item_if_absent({"PK": "X#1", "SK": "S", "v": "first"}) is True
    assert dynamodb.get_item("X#1", "S") == {"PK": "X#1", "SK": "S", "v": "first"}


def test_put_if_absent_leaves_an_existing_item_untouched(
    dynamodb_table: DynamoDBClient,
) -> None:
    dynamodb.put_item_if_absent({"PK": "X#1", "SK": "S", "v": "first"})

    assert dynamodb.put_item_if_absent({"PK": "X#1", "SK": "S", "v": "second"}) is False
    assert dynamodb.get_item("X#1", "S") == {"PK": "X#1", "SK": "S", "v": "first"}


def test_put_if_absent_raises_failures_other_than_a_taken_key(
    dynamodb_table: DynamoDBClient,
) -> None:
    # A throttle or permission error must not read as "already claimed", or a delivery
    # that was never processed would be acknowledged and lost.
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "PutItem"
    )
    with patch.object(dynamodb, "get_client") as client:
        client.return_value.put_item.side_effect = denied
        with pytest.raises(ClientError, match="denied"):
            dynamodb.put_item_if_absent({"PK": "X#1", "SK": "S"})


# ── claim_update ─────────────────────────────────────────────────────────────


def test_first_claim_succeeds_and_writes_an_expiring_marker(
    dynamodb_table: DynamoDBClient,
) -> None:
    before = int(datetime.now(timezone.utc).timestamp())
    assert claim_update(4242) is True
    after = int(datetime.now(timezone.utc).timestamp())

    marker = dynamodb.get_item(f"{UPDATE_PK_PREFIX}4242", UPDATE_MARKER_SK)
    assert marker is not None
    retention = settings.UPDATE_DEDUP_TTL_SECONDS
    assert before + retention <= marker[dynamodb.TTL_ATTRIBUTE] <= after + retention


def test_second_claim_of_the_same_update_fails(dynamodb_table: DynamoDBClient) -> None:
    claim_update(4242)

    assert claim_update(4242) is False


def test_claims_of_different_updates_are_independent(
    dynamodb_table: DynamoDBClient,
) -> None:
    assert claim_update(4242) is True
    assert claim_update(4243) is True


# ── lambda_handler ───────────────────────────────────────────────────────────


def test_first_delivery_is_processed(
    dynamodb_table: DynamoDBClient, processed: list[dict[str, Any]]
) -> None:
    body = {"update_id": 7, "message": {"text": "coffee 5"}}

    assert lambda_handler(_delivery(body), None)["statusCode"] == 200
    assert processed == [body]


def test_redelivery_is_acknowledged_but_not_processed_again(
    dynamodb_table: DynamoDBClient, processed: list[dict[str, Any]]
) -> None:
    # The production failure: Telegram resends the same update_id after a 503.
    body = {"update_id": 7, "message": {"text": "coffee 5"}}
    lambda_handler(_delivery(body), None)

    assert lambda_handler(_delivery(body), None)["statusCode"] == 200
    assert processed == [body]


def test_delivery_without_an_update_id_is_dropped(
    dynamodb_table: DynamoDBClient, processed: list[dict[str, Any]]
) -> None:
    assert lambda_handler(_delivery({"message": {}}), None)["statusCode"] == 200
    assert processed == []


def test_forged_delivery_claims_nothing(
    dynamodb_table: DynamoDBClient, processed: list[dict[str, Any]]
) -> None:
    # Authentication runs first, so a forger cannot burn a genuine update's id and get
    # the real delivery dropped as a "redelivery".
    forged = {"headers": {_SECRET_TOKEN_HEADER: "wrong"}, "body": '{"update_id": 7}'}

    assert lambda_handler(forged, None)["statusCode"] == 403
    assert dynamodb.get_item(f"{UPDATE_PK_PREFIX}7", UPDATE_MARKER_SK) is None
