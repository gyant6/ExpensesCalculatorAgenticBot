from src.bot.storage import dynamodb
from src.bot.tools import expenses

import pytest 

from datetime import datetime, timezone
from unittest.mock import patch

TELEGRAM_USER_ID = "123456"


@pytest.fixture
def base_expense():
    return {
        "source_message": "Breakfast at Yakun $6.13",
        "summary": "Breakfast at Yakun",
        "category": "Food",
        "amount": "6.13",
        "currency": "SGD",
        "date": "2023-12-20"
    }


def test_add_expense(dynamodb_table, base_expense):    
    with patch('src.bot.tools.expenses.datetime') as mock_dt:       
        mock_dt.now.return_value = datetime(2023, 12, 20, 13, 45, 50, tzinfo=timezone.utc)
        mock_dt.strptime.side_effect = datetime.strptime
        datetime_now = mock_dt.now().isoformat()

        tool_output = expenses.add_expense.invoke({
            **base_expense,
            "telegram_user_id": TELEGRAM_USER_ID,
            "payment_method": "Card",
        })
   
    assert tool_output == "Expense recorded."
    
    item = dynamodb.get_item(f"USER#{TELEGRAM_USER_ID}", f"EXPENSE#{datetime_now}")
    assert item == {
        **base_expense,
        "PK": f"USER#{TELEGRAM_USER_ID}",
        "SK": f"EXPENSE#{datetime_now}",
        "payment_method": "Card",
        "updated_at": datetime_now
    }


def test_add_expense_default_payment_method(dynamodb_table, base_expense):    
    with patch('src.bot.tools.expenses.datetime') as mock_dt:       
        mock_dt.now.return_value = datetime(2023, 12, 20, 13, 45, 50, tzinfo=timezone.utc)
        mock_dt.strptime.side_effect = datetime.strptime
        datetime_now = mock_dt.now().isoformat()

        tool_output = expenses.add_expense.invoke({
            **base_expense,
            "telegram_user_id": TELEGRAM_USER_ID
        })
   
    assert tool_output == "Expense recorded."
    
    item = dynamodb.get_item(f"USER#{TELEGRAM_USER_ID}", f"EXPENSE#{datetime_now}")
    assert item == {
        **base_expense,
        "PK": f"USER#{TELEGRAM_USER_ID}",
        "SK": f"EXPENSE#{datetime_now}",
        "payment_method": "Cash",
        "updated_at": datetime_now
    }


def test_add_expense_invalid_date_format(base_expense):
    tool_output = expenses.add_expense.invoke({
        **base_expense,
        "telegram_user_id": TELEGRAM_USER_ID,
        "date": "2023-13-20"
    })
    
    assert tool_output == "datetime should be in YYYY-MM-DD format (e.g. 2020-12-30)"


def test_add_expense_invalid_amount(base_expense):
    tool_output = expenses.add_expense.invoke({
        **base_expense,
        "telegram_user_id": TELEGRAM_USER_ID,
        "amount": "invalid_amount"
    })
    
    assert tool_output == "amount must be a valid positive number (e.g. '1200' or '12.50')"


def test_add_expense_zero_amount(base_expense):
    tool_output = expenses.add_expense.invoke({
        **base_expense,
        "telegram_user_id": TELEGRAM_USER_ID,
        "amount": "0"
    })
    
    assert tool_output == "amount should be greater than 0"


def test_add_expense_negative_amount(base_expense):
    tool_output = expenses.add_expense.invoke({
        **base_expense,
        "telegram_user_id": TELEGRAM_USER_ID,
        "amount": "-1.35"
    })
    
    assert tool_output == "amount should be greater than 0"


def test_add_expense_invalid_category(base_expense):
    tool_output = expenses.add_expense.invoke({
        **base_expense,
        "telegram_user_id": TELEGRAM_USER_ID,
        "category": "Casino"
    })
    
    assert tool_output == "category should be one of ['Accommodation', 'Car Rental', 'Flight', 'Food', 'Insurance', 'Leisure', 'Misc', 'Shopping', 'Transport']"
