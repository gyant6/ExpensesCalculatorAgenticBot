"""Shared setup for tests that run against DynamoDB Local.

These tests are opt-in: they need the docker compose DynamoDB Local container, so each
module skips itself unless RUN_DYNAMODB_LOCAL_TESTS=1.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Final
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
from mypy_boto3_dynamodb import DynamoDBClient

from src.bot.config import settings

OPT_IN_VARIABLE: Final = "RUN_DYNAMODB_LOCAL_TESTS"
ENDPOINT_VARIABLE: Final = "DYNAMODB_LOCAL_TEST_ENDPOINT"
DEFAULT_ENDPOINT: Final = "http://127.0.0.1:8000"
_LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})

requires_dynamodb_local = pytest.mark.skipif(
    os.environ.get(OPT_IN_VARIABLE) != "1",
    reason=f"Set {OPT_IN_VARIABLE}=1 to run against DynamoDB Local",
)


def _local_endpoint() -> str:
    """Return the DynamoDB Local endpoint, refusing anything but a loopback HTTP address.

    Returns:
        The endpoint URL from DYNAMODB_LOCAL_TEST_ENDPOINT, or the docker compose default.

    Raises:
        pytest.fail.Exception: If the endpoint could reach a non-local database.
    """
    endpoint = os.environ.get(ENDPOINT_VARIABLE, DEFAULT_ENDPOINT)
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        pytest.fail(f"{ENDPOINT_VARIABLE} must be a local HTTP address")
    return endpoint


@pytest.fixture
def local_table(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Create and later remove only this test's uniquely named local table.

    Points the application settings at the new table, and pins the AWS credential chain
    to dummy values so no AWS account or profile is ever used.

    Yields:
        Nothing; the table exists for the duration of the test.

    Raises:
        pytest.fail.Exception: If the endpoint is not local or DynamoDB Local is down.
    """
    endpoint = _local_endpoint()

    # The application creates its own boto3 clients, so the whole credential chain must
    # be pinned before any of them is built.
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
