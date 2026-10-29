from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

from src.bot import trip_analysis
from src.bot.config import settings
from src.bot.storage import dynamodb

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient


@pytest.fixture(autouse=True)
def no_trip_insights(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Keep the post-trip insights model off, whatever a developer's .env says.

    The setting is optional and a local .env could name a model, which would make every
    test that ends a trip a paid Bedrock call. Tests that want insights supply their own.
    """
    monkeypatch.setattr(settings, "AWS_BEDROCK_ANALYSIS_MODEL_ID", None)
    trip_analysis.reset_model()
    yield
    trip_analysis.reset_model()


@pytest.fixture(autouse=True)
def fresh_dynamodb_client() -> Generator[None, None, None]:
    """Give every test its own shared DynamoDB client.

    The application reuses one client, so without this a client built under one test's
    mock or credentials would serve the next test.
    """
    dynamodb.reset_clients()
    yield
    dynamodb.reset_clients()


@pytest.fixture(scope="function")
def dynamodb_table(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[DynamoDBClient, None, None]:
    monkeypatch.setattr(settings, "DYNAMODB_ENDPOINT_URL", None)
    with mock_aws():
        client = boto3.client(
            "dynamodb",
            endpoint_url=settings.DYNAMODB_ENDPOINT_URL,
            region_name=settings.AWS_REGION,
        )
        client.create_table(
            TableName=settings.DYNAMODB_TABLE_NAME,
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
        yield client


@pytest.fixture(scope="function")
def base_expense() -> dict[str, Any]:
    return {
        "source_message": "Breakfast at Yakun $6.13",
        "summary": "Breakfast at Yakun",
        "category": "Food",
        "amount": "6.13",
        "currency": "SGD",
        "date": "2023-12-20",
    }
