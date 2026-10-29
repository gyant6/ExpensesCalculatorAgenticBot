"""Low-level DynamoDB client wrapper for the expenses bot single-table design.

All read operations deserialise DynamoDB's typed wire format into plain Python
dicts using TypeDeserializer. The table name and endpoint are read from
application settings so the same code works against DynamoDB Local and real AWS.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import boto3.session
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from src.bot.config import settings

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

deserializer = TypeDeserializer()
serializer = TypeSerializer()

# Must match `ttl.attribute_name` on the table in terraform/main.tf. DynamoDB deletes an
# item some time after the epoch-seconds value in this attribute has passed.
TTL_ATTRIBUTE: Final = "ttl"

# Error code DynamoDB returns when a write's ConditionExpression evaluates false.
_CONDITION_FAILED: Final = "ConditionalCheckFailedException"

# A single-item write that hits an item while a transaction is committing on it fails
# with this code. A cancelled transaction instead reports one reason per item, and
# these are the two reasons acted on.
_TRANSACTION_CONFLICT: Final = "TransactionConflictException"
_TRANSACTION_CANCELLED: Final = "TransactionCanceledException"
_REASON_CONFLICT: Final = "TransactionConflict"
_REASON_CONDITION_FAILED: Final = "ConditionalCheckFailed"

# A conflict clears once the other transaction commits, within milliseconds. Four
# attempts with a growing pause wait at most 0.3 s in total.
_CONFLICT_ATTEMPTS: Final = 4
_CONFLICT_BACKOFF_SECONDS: Final = 0.05


_clients: dict[tuple[str | None, str], DynamoDBClient] = {}
_clients_lock = threading.Lock()


def get_client() -> DynamoDBClient:
    """Return the shared DynamoDB client for the configured endpoint and region.

    The client is created once and reused. A boto3 client is safe to share between
    threads, but creating one is not: boto3.client() builds on a default session that
    is not thread-safe, and LangGraph's tool node runs a turn's tool calls — two
    add_expense calls for "coffee 5, taxi 12" — on parallel threads. Creation therefore
    happens under a lock, from a session of its own. Reuse also skips loading the
    service model and resolving credentials on every request. Credentials from the
    Lambda role refresh inside the client, so a long-lived client does not go stale.

    Clients are keyed by endpoint and region, which tests change; production uses one.
    In production, connects to real DynamoDB in the configured region. Locally, routes
    to DynamoDB Local via DYNAMODB_ENDPOINT_URL if set.

    Returns:
        A boto3 DynamoDB client.
    """
    key = (settings.DYNAMODB_ENDPOINT_URL, settings.AWS_REGION)
    client = _clients.get(key)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = boto3.session.Session().client(
                "dynamodb", endpoint_url=key[0], region_name=key[1]
            )
            _clients[key] = client
    return client


def reset_clients() -> None:
    """Discard the shared clients so the next get_client builds a new one.

    For tests, which change credentials or start a new mock between cases. Production
    code has no reason to call it.
    """
    with _clients_lock:
        _clients.clear()


def _expected_state_condition(
    expected: dict[str, Any],
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Build a ConditionExpression requiring an item to still hold the given values.

    Args:
        expected: Attribute names mapped to the value each must currently have. None
            means the attribute must be absent, for items written before it existed.

    Returns:
        The condition expression, its attribute name placeholders, and its serialised
        value placeholders. Value placeholders are prefixed `:expected_` so they cannot
        collide with those of an update expression.

    Raises:
        ValueError: If expected is empty, which would make the condition meaningless.
    """
    if not expected:
        raise ValueError("expected must not be empty")

    clauses: list[str] = []
    values: dict[str, Any] = {}
    for name, value in expected.items():
        if value is None:
            clauses.append(f"attribute_not_exists(#{name})")
        else:
            clauses.append(f"#{name} = :expected_{name}")
            values[f":expected_{name}"] = serializer.serialize(value)
    names = {f"#{name}": name for name in expected}
    return " AND ".join(clauses), names, values


def update_item(pk: str, sk: str, fields: dict[str, Any]) -> None:
    """Update specific attributes of an existing DynamoDB item without overwriting the whole item.

    Builds a SET UpdateExpression dynamically from the provided fields dict. Only the
    supplied attributes are modified; all other attributes on the item are untouched.
    This is atomic at the DynamoDB level — no read is required before calling this.

    Args:
        pk: Partition key value (e.g. 'USER#123456789').
        sk: Sort key value (e.g. 'EXPENSE#2026-06-04T14:32:05.123456+00:00').
        fields: Dict of attribute names to their new Python-native values. Must not be empty.

    Raises:
        ValueError: If fields is empty.
        botocore.exceptions.ClientError: If the DynamoDB request fails.
            If the item does not exist, the error code will be 'ConditionalCheckFailedException'.
    """
    if not fields:
        raise ValueError("fields must not be empty")

    update_exp_str = "SET" + ",".join(
        [f" #{field} = :{field}" for field in fields.keys()]
    )
    exp_attr_names = {f"#{field}": f"{field}" for field in fields.keys()}
    exp_attr_values = {f":{k}": serializer.serialize(v) for k, v in fields.items()}

    _send_write(
        get_client().update_item,
        TableName=settings.DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        UpdateExpression=update_exp_str,
        ExpressionAttributeNames=exp_attr_names,
        ExpressionAttributeValues=exp_attr_values,
        ConditionExpression="attribute_exists(PK)",
    )


def _update_if_unchanged_request(
    pk: str, sk: str, fields: dict[str, Any], expected: dict[str, Any]
) -> dict[str, Any]:
    """Build an UpdateItem request applied only if the item still matches `expected`.

    The same shape serves a plain update_item call and an Update inside a transaction.

    Raises:
        ValueError: If fields or expected is empty.
    """
    if not fields:
        raise ValueError("fields must not be empty")
    condition, condition_names, condition_values = _expected_state_condition(expected)
    return {
        "TableName": settings.DYNAMODB_TABLE_NAME,
        "Key": {"PK": {"S": pk}, "SK": {"S": sk}},
        "UpdateExpression": "SET " + ", ".join(f"#{name} = :{name}" for name in fields),
        "ConditionExpression": f"attribute_exists(PK) AND {condition}",
        "ExpressionAttributeNames": {
            **{f"#{name}": name for name in fields},
            **condition_names,
        },
        "ExpressionAttributeValues": {
            **{f":{name}": serializer.serialize(v) for name, v in fields.items()},
            **condition_values,
        },
    }


def _delete_if_unchanged_request(
    pk: str, sk: str, expected: dict[str, Any]
) -> dict[str, Any]:
    """Build a DeleteItem request applied only if the item still matches `expected`.

    Raises:
        ValueError: If expected is empty.
    """
    condition, condition_names, condition_values = _expected_state_condition(expected)
    request: dict[str, Any] = {
        "TableName": settings.DYNAMODB_TABLE_NAME,
        "Key": {"PK": {"S": pk}, "SK": {"S": sk}},
        "ConditionExpression": f"attribute_exists(PK) AND {condition}",
        "ExpressionAttributeNames": condition_names,
    }
    # DynamoDB rejects an empty ExpressionAttributeValues, which an all-absent
    # expectation would produce.
    if condition_values:
        request["ExpressionAttributeValues"] = condition_values
    return request


def _put_if_absent_request(item: dict[str, Any]) -> dict[str, Any]:
    """Build a PutItem request applied only if no item exists at the item's key."""
    return {
        "TableName": settings.DYNAMODB_TABLE_NAME,
        "Item": {k: serializer.serialize(v) for k, v in item.items()},
        "ConditionExpression": "attribute_not_exists(PK)",
    }


def _send_write(operation: Callable[..., object], **request: Any) -> None:
    """Send a single-item write, retrying while it collides with a transaction.

    A transaction holds its items while it commits, and a plain write to one of them in
    that moment fails with TransactionConflictException. The collision is transient —
    the transaction finishes within milliseconds — but botocore does not retry it.

    Args:
        operation: The client method to call, e.g. get_client().put_item.
        request: Its keyword arguments.

    Raises:
        botocore.exceptions.ClientError: If the write fails, including when it still
            conflicts after _CONFLICT_ATTEMPTS attempts.
    """
    attempt = 1
    while True:
        try:
            operation(**request)
            return
        except ClientError as exc:
            if (
                exc.response["Error"]["Code"] != _TRANSACTION_CONFLICT
                or attempt >= _CONFLICT_ATTEMPTS
            ):
                raise
        time.sleep(_CONFLICT_BACKOFF_SECONDS * attempt)
        attempt += 1


def update_item_if_unchanged(
    pk: str, sk: str, fields: dict[str, Any], expected: dict[str, Any]
) -> bool:
    """Update attributes of an item only if it still holds the values it was read with.

    This is optimistic locking: the caller reads the item, decides on a change, and
    passes the values it read as `expected`. DynamoDB checks them and applies the update
    as one atomic operation, so if another request changed or deleted the item in
    between, this update is refused instead of overwriting that change.

    Args:
        pk: Partition key value (e.g. 'USER#123456789').
        sk: Sort key value (e.g. 'EXPENSE#k7qm').
        fields: Attribute names mapped to their new Python-native values. Must not be
            empty.
        expected: Attribute names mapped to the value each must currently have; None
            means the attribute must be absent. Must not be empty.

    Returns:
        True if the update was applied, False if the item no longer matched `expected`
        or no longer exists, in which case nothing was changed.

    Raises:
        ValueError: If fields or expected is empty.
        botocore.exceptions.ClientError: If the request fails for any reason other than
            the condition not holding.
    """
    request = _update_if_unchanged_request(pk, sk, fields, expected)
    try:
        _send_write(get_client().update_item, **request)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == _CONDITION_FAILED:
            return False
        raise
    return True


class WriteOutcome(StrEnum):
    """What a guarded write did.

    Members:
        APPLIED: The guard allowed the write and it was made.
        ITEM_CONDITION_FAILED: The written item's own condition failed — for a put, an
            item already existed at its key; for an update or delete, the item no longer
            matched `expected` or no longer exists. Nothing was written.
        GUARD_FAILED: The guard item held the forbidden value. Nothing was written.
    """

    APPLIED = "applied"
    ITEM_CONDITION_FAILED = "item_condition_failed"
    GUARD_FAILED = "guard_failed"


@dataclass(frozen=True)
class Guard:
    """Another item whose state must allow a write, checked in the same transaction.

    Attributes:
        pk: Partition key of the item checked.
        sk: Sort key of the item checked.
        attribute: The attribute checked.
        forbidden_value: The write is refused while the attribute holds this value. A
            missing item or attribute allows it.
    """

    pk: str
    sk: str
    attribute: str
    forbidden_value: Any


def _condition_check(guard: Guard) -> dict[str, Any]:
    """Build the transaction's ConditionCheck for a guard."""
    return {
        "TableName": settings.DYNAMODB_TABLE_NAME,
        "Key": {"PK": {"S": guard.pk}, "SK": {"S": guard.sk}},
        "ConditionExpression": (
            "attribute_not_exists(#guarded) OR #guarded <> :forbidden"
        ),
        "ExpressionAttributeNames": {"#guarded": guard.attribute},
        "ExpressionAttributeValues": {
            ":forbidden": serializer.serialize(guard.forbidden_value)
        },
    }


def _transact(guard: Guard, write: dict[str, dict[str, Any]]) -> WriteOutcome:
    """Check a guard and make one write as a single atomic transaction.

    A write cannot slip in after the guard item changes, as it could with a read
    followed by a write: DynamoDB applies both or neither, and orders the transaction
    against any concurrent write to the guard item.

    Args:
        guard: The item state that must allow the write.
        write: One transaction item, e.g. {"Put": <PutItem request>}.

    Returns:
        Which condition, if any, stopped the write.

    Raises:
        botocore.exceptions.ClientError: If the transaction fails for any reason other
            than a condition, including a conflict that persists for _CONFLICT_ATTEMPTS
            attempts.
    """
    transact_items: list[Any] = [{"ConditionCheck": _condition_check(guard)}, write]
    attempt = 1
    while True:
        try:
            get_client().transact_write_items(TransactItems=transact_items)
            return WriteOutcome.APPLIED
        except ClientError as exc:
            if exc.response["Error"]["Code"] != _TRANSACTION_CANCELLED:
                raise
            # One reason per transaction item, in order: the guard, then the write.
            reasons = [
                reason.get("Code")
                for reason in exc.response.get("CancellationReasons", [])
            ]
            if reasons[:1] == [_REASON_CONDITION_FAILED]:
                return WriteOutcome.GUARD_FAILED
            if reasons[1:2] == [_REASON_CONDITION_FAILED]:
                return WriteOutcome.ITEM_CONDITION_FAILED
            if _REASON_CONFLICT not in reasons or attempt >= _CONFLICT_ATTEMPTS:
                raise
        time.sleep(_CONFLICT_BACKOFF_SECONDS * attempt)
        attempt += 1


def guarded_put_if_absent(item: dict[str, Any], guard: Guard) -> WriteOutcome:
    """Write an item if its key is free and the guard allows it, atomically.

    Args:
        item: The full item as a plain Python dict, PK and SK included.
        guard: The item state that must allow the write.

    Returns:
        APPLIED, ITEM_CONDITION_FAILED if an item already exists at the key, or
        GUARD_FAILED.

    Raises:
        botocore.exceptions.ClientError: If the transaction fails for another reason.
    """
    return _transact(guard, {"Put": _put_if_absent_request(item)})


def guarded_update_if_unchanged(
    pk: str,
    sk: str,
    fields: dict[str, Any],
    expected: dict[str, Any],
    guard: Guard,
) -> WriteOutcome:
    """update_item_if_unchanged, made only if the guard allows it, atomically.

    Args:
        pk: Partition key value of the item to update.
        sk: Sort key value of the item to update.
        fields: Attribute names mapped to their new values. Must not be empty.
        expected: Attribute names mapped to the value each must currently have; None
            means absent. Must not be empty.
        guard: The item state that must allow the write.

    Returns:
        APPLIED, ITEM_CONDITION_FAILED if the item changed or is gone, or GUARD_FAILED.

    Raises:
        ValueError: If fields or expected is empty.
        botocore.exceptions.ClientError: If the transaction fails for another reason.
    """
    return _transact(
        guard, {"Update": _update_if_unchanged_request(pk, sk, fields, expected)}
    )


def guarded_delete_if_unchanged(
    pk: str, sk: str, expected: dict[str, Any], guard: Guard
) -> WriteOutcome:
    """Delete an item if it still matches `expected` and the guard allows it, atomically.

    Args:
        pk: Partition key value of the item to delete.
        sk: Sort key value of the item to delete.
        expected: Attribute names mapped to the value each must currently have; None
            means absent. Must not be empty.
        guard: The item state that must allow the write.

    Returns:
        APPLIED, ITEM_CONDITION_FAILED if the item changed or is gone, or GUARD_FAILED.

    Raises:
        ValueError: If expected is empty.
        botocore.exceptions.ClientError: If the transaction fails for another reason.
    """
    return _transact(guard, {"Delete": _delete_if_unchanged_request(pk, sk, expected)})


def put_item(item: dict[str, Any]) -> None:
    """Write an item to the DynamoDB table, overwriting any existing item at the same key.

    Args:
        item: The full item to write as a plain Python dict. PK and SK must be
              included. All values must be Python-native types — serialisation
              to DynamoDB wire format is handled internally.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    low_level_data = {k: serializer.serialize(v) for k, v in item.items()}
    _send_write(
        get_client().put_item,
        TableName=settings.DYNAMODB_TABLE_NAME,
        Item=low_level_data,
    )


def put_item_if_absent(item: dict[str, Any]) -> bool:
    """Write an item only if no item already exists at its primary key.

    The check and the write are one atomic DynamoDB operation, so of two concurrent
    callers writing the same key exactly one succeeds — which makes this usable as a
    claim, not just an insert.

    Args:
        item: The full item to write as a plain Python dict. PK and SK must be included,
            and all values must be Python-native types.

    Returns:
        True if the item was written, False if an item already existed at that key, in
        which case the existing item is left untouched.

    Raises:
        botocore.exceptions.ClientError: If the request fails for any reason other than
            the key already being taken.
    """
    try:
        _send_write(get_client().put_item, **_put_if_absent_request(item))
    except ClientError as exc:
        if exc.response["Error"]["Code"] == _CONDITION_FAILED:
            return False
        raise
    return True


def get_item(
    pk: str, sk: str, *, consistent_read: bool = False
) -> dict[str, Any] | None:
    """Fetch a single item from DynamoDB by its primary key.

    Args:
        pk: Partition key value (e.g. 'USER#123456789').
        sk: Sort key value (e.g. 'EXPENSE#2026-06-04T14:32:05.123456+00:00').
        consistent_read: Read the latest committed value when verifying a write.

    Returns:
        The item as a plain dict, or None if no item exists at that key.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    response = get_client().get_item(
        TableName=settings.DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        ConsistentRead=consistent_read,
    )

    low_level_data = response.get("Item")

    if low_level_data is not None:
        items = {k: deserializer.deserialize(v) for k, v in low_level_data.items()}
        return items

    return None


def delete_item(pk: str, sk: str) -> None:
    """Delete a single item from the DynamoDB table by its primary key.

    Args:
        pk: Partition key value (e.g. 'USER#123456789').
        sk: Sort key value (e.g. 'EXPENSE#2026-06-04T14:32:05.123456+00:00').

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    _send_write(
        get_client().delete_item,
        TableName=settings.DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
    )


def scan_by_pk_prefix(prefix: str) -> list[dict[str, Any]]:
    """Scan the table for all items whose partition key starts with a given prefix.

    Unlike query_by_prefix, this crosses partition key boundaries, so it requires a full
    table scan with a filter. Only suitable for admin operations on small result sets
    (e.g. listing all AUTH# records) — never use for hot paths.

    Args:
        prefix: The PK prefix to filter by (e.g. 'AUTH#').

    Returns:
        List of matching items as plain Python dicts. Empty list if none found.

    Raises:
        botocore.exceptions.ClientError: If any page of the DynamoDB scan fails.
    """
    pages = (
        get_client()
        .get_paginator("scan")
        .paginate(
            TableName=settings.DYNAMODB_TABLE_NAME,
            FilterExpression="begins_with(PK, :prefix)",
            ExpressionAttributeValues={":prefix": {"S": prefix}},
        )
    )

    return [
        {k: deserializer.deserialize(v) for k, v in item.items()}
        for page in pages
        for item in page.get("Items", [])
    ]


def query_by_prefix(
    pk: str, prefix: str, *, consistent_read: bool = False
) -> list[dict[str, Any]]:
    """Fetch all items for a partition key whose sort key starts with a given prefix.

    Used to retrieve all expenses for a user (prefix='EXPENSE#') or to check
    for an active trip (prefix='TRIP#').

    A single DynamoDB query returns at most 1 MB of data, signalling that more remains by
    returning a LastEvaluatedKey. This follows that key to exhaustion via the boto3
    paginator, so callers always receive the whole partition instead of a silently
    truncated first page: `end_trip` archives and deletes every expense, and
    `get_all_expenses` lists every one.

    Args:
        pk: Partition key value (e.g. 'USER#123456789').
        prefix: Sort key prefix to filter by (e.g. 'EXPENSE#').
        consistent_read: Include every write acknowledged before the query, rather than
            possibly missing one from the last second. Costs twice the read capacity.

    Returns:
        List of matching items as plain Python dicts, in sort key order. Empty list if
        none found.

    Raises:
        botocore.exceptions.ClientError: If any page of the DynamoDB query fails.
    """
    pages = (
        get_client()
        .get_paginator("query")
        .paginate(
            TableName=settings.DYNAMODB_TABLE_NAME,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={":pk": {"S": pk}, ":prefix": {"S": prefix}},
            ConsistentRead=consistent_read,
        )
    )

    return [
        {k: deserializer.deserialize(v) for k, v in item.items()}
        for page in pages
        for item in page.get("Items", [])
    ]
