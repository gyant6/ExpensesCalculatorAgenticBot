import boto3
import pytest

from src.bot.config import settings

from moto import mock_aws


@pytest.fixture(scope="function")
def dynamodb_table(monkeypatch):
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
def base_expense():
    return {
        "source_message": "Breakfast at Yakun $6.13",
        "summary": "Breakfast at Yakun",
        "category": "Food",
        "amount": "6.13",
        "currency": "SGD",
        "date": "2023-12-20",
    }
