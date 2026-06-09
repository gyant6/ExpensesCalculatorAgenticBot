import pytest

from src.bot.storage import dynamodb

from botocore.exceptions import ClientError


def test_update_item_updates_existing_item(dynamodb_table):
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1"
    }
    
    dynamodb.put_item(item)
    
    update = {"amount": "1.50"}
    dynamodb.update_item(item["PK"], item["SK"], update)
    updated_item = dynamodb.get_item(item["PK"], item["SK"])
    
    assert item | update == updated_item
    
    
def test_update_item_raises_on_nonexistent_item(dynamodb_table):
    with pytest.raises(ClientError) as exc_info:
        dynamodb.update_item("unknown", "unknown", {"unknown": "unknown"})
        
    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    
    
def test_update_item_raises_on_empty_fields():
    with pytest.raises(ValueError):
        dynamodb.update_item("unknown", "unknown", {})


def test_put_item(dynamodb_table):
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1"
    }
    
    dynamodb.put_item(item)
    
    assert item == dynamodb.get_item(item["PK"], item["SK"])


def test_transact_write_delete_put_deletes_old_and_creates_new(dynamodb_table):
    item1 = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1"
    }
    
    dynamodb.put_item(item1)
    
    item2 = {**item1, "SK": "EXPENSE#2"}
    
    dynamodb.transact_write_delete_put(item1["PK"], item1["SK"], item2)
    assert dynamodb.get_item(item1["PK"], item1["SK"]) is None
    assert item2 == dynamodb.get_item(item2["PK"], item2["SK"])


def test_get_item_returns_none_when_not_found(dynamodb_table):
    item = dynamodb.get_item("Unknown", "Unknown")
    assert item is None


def test_get_item_returns_item_when_found(dynamodb_table):
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1"
    }
    dynamodb.put_item(item)
    assert item == dynamodb.get_item(item["PK"], item["SK"])


def test_delete_existing_item(dynamodb_table):
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1"
    }
    dynamodb.put_item(item)
    dynamodb.delete_item(item["PK"], item["SK"])
    assert dynamodb.get_item(item["PK"], item["SK"]) is None
    
    
def test_delete_nonexistent_item(dynamodb_table):
    dynamodb.delete_item("Unknown", "Unknown")
    

def test_query_by_prefix_returns_empty_list(dynamodb_table):
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1"
    }

    assert dynamodb.query_by_prefix(item["PK"], "EXPENSE#") == []


def test_query_by_prefix_returns_single_item(dynamodb_table):
    item = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1"
    }
    dynamodb.put_item(item)

    assert dynamodb.query_by_prefix(item["PK"], "EXPENSE#") == [item]


def test_query_by_prefix_returns_multiple_items(dynamodb_table):
    item1 = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1"
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
        "source_message": "Lunch at Sushi Tei $2"
    }
    dynamodb.put_item(item1)
    dynamodb.put_item(item2)
    result = dynamodb.query_by_prefix("USER#00000000", "EXPENSE#")

    assert sorted(result, key=lambda x: x["SK"]) == sorted([item1, item2], key=lambda x: x["SK"])


def test_query_by_prefix_returns_only_matching_items(dynamodb_table):
    expense = {
        "PK": "USER#00000000",
        "SK": "EXPENSE#1",
        "summary": "Lunch at Sushi Tei",
        "category": "Food",
        "amount": "1.00",
        "currency": "SGD",
        "date": "2020-12-31",
        "payment_method": "Cash",
        "source_message": "Lunch at Sushi Tei $1"
    }
    trip = {
        "PK": "USER#00000000",
        "SK": "TRIP#1"
    }
    dynamodb.put_item(expense)
    dynamodb.put_item(trip)

    assert dynamodb.query_by_prefix("USER#00000000", "EXPENSE#") == [expense]
