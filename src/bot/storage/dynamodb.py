"""Low-level DynamoDB client wrapper for the expenses bot single-table design.

All read operations deserialise DynamoDB's typed wire format into plain Python
dicts using TypeDeserializer. The table name and endpoint are read from
application settings so the same code works against DynamoDB Local and real AWS.
"""

import boto3

from src.bot.config import settings

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer


deserializer = TypeDeserializer()
serializer = TypeSerializer()


def get_client():
    """Create and return a boto3 DynamoDB client configured from application settings.

    Returns a new client on each call so that the mock context is respected in tests.
    In production, connects to real DynamoDB using the region from settings. Locally,
    routes to DynamoDB Local via DYNAMODB_ENDPOINT_URL if set.

    Returns:
        A boto3 DynamoDB client.
    """
    return boto3.client(
        "dynamodb",
        endpoint_url=settings.DYNAMODB_ENDPOINT_URL,
        region_name=settings.AWS_REGION,
    )


def update_item(pk: str, sk: str, fields: dict) -> None:
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

    get_client().update_item(
        TableName=settings.DYNAMODB_TABLE_NAME,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        UpdateExpression=update_exp_str,
        ExpressionAttributeNames=exp_attr_names,
        ExpressionAttributeValues=exp_attr_values,
        ConditionExpression="attribute_exists(PK)",
    )


def put_item(item: dict) -> None:
    """Write an item to the DynamoDB table, overwriting any existing item at the same key.

    Args:
        item: The full item to write as a plain Python dict. PK and SK must be
              included. All values must be Python-native types — serialisation
              to DynamoDB wire format is handled internally.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    low_level_data = {k: serializer.serialize(v) for k, v in item.items()}
    get_client().put_item(TableName=settings.DYNAMODB_TABLE_NAME, Item=low_level_data)


def transact_write_delete_put(pk: str, sk: str, item: dict) -> None:
    """Atomically delete one item and put another in a single DynamoDB transaction.

    Used when an expense's SK must change (i.e. date edit), where the old item must
    be deleted and a new item written under the new SK. Both operations succeed or
    both are rolled back — the expense is never left in a partially updated state.

    Args:
        pk: Partition key of the item to delete (e.g. 'USER#123456789').
        sk: Sort key of the item to delete (e.g. 'EXPENSE#2026-06-04T14:32:05.123456+00:00').
        item: The full new item to write as a plain Python dict. Must include PK and SK.
                All values must be Python-native types — serialisation is handled internally.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB transaction fails.
            If the item to delete does not exist, DynamoDB will still succeed — no
            condition check is applied on the delete.
    """
    get_client().transact_write_items(
        TransactItems=[
            {
                "Delete": {
                    "TableName": settings.DYNAMODB_TABLE_NAME,
                    "Key": {"PK": {"S": pk}, "SK": {"S": sk}},
                }
            },
            {
                "Put": {
                    "TableName": settings.DYNAMODB_TABLE_NAME,
                    "Item": {k: serializer.serialize(v) for k, v in item.items()},
                }
            },
        ]
    )


def get_item(pk: str, sk: str) -> dict | None:
    """Fetch a single item from DynamoDB by its primary key.

    Args:
        pk: Partition key value (e.g. 'USER#123456789').
        sk: Sort key value (e.g. 'EXPENSE#2026-06-04T14:32:05.123456+00:00').

    Returns:
        The item as a plain dict, or None if no item exists at that key.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    response = get_client().get_item(
        TableName=settings.DYNAMODB_TABLE_NAME, Key={"PK": {"S": pk}, "SK": {"S": sk}}
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
    get_client().delete_item(
        TableName=settings.DYNAMODB_TABLE_NAME, Key={"PK": {"S": pk}, "SK": {"S": sk}}
    )


def query_by_prefix(pk: str, prefix: str) -> list[dict]:
    """Fetch all items for a partition key whose sort key starts with a given prefix.

    Used to retrieve all expenses for a user (prefix='EXPENSE#') or to check
    for an active trip (prefix='TRIP#').

    Args:
        pk: Partition key value (e.g. 'USER#123456789').
        prefix: Sort key prefix to filter by (e.g. 'EXPENSE#').

    Returns:
        List of matching items as plain Python dicts. Empty list if none found.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    response = get_client().query(
        TableName=settings.DYNAMODB_TABLE_NAME,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={":pk": {"S": pk}, ":prefix": {"S": prefix}},
    )

    low_level_data = response.get("Items", [])

    items = [
        {k: deserializer.deserialize(v) for k, v in item.items()}
        for item in low_level_data
    ]
    return items
