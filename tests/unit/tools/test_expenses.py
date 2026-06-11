from src.bot.storage import dynamodb
from src.bot.tools import expenses


from datetime import datetime, timezone
from unittest.mock import patch

TELEGRAM_USER_ID = "123456"


def test_add_expense(dynamodb_table, base_expense):
    with patch("src.bot.tools.expenses.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(
            2023, 12, 20, 13, 45, 50, 123456, tzinfo=timezone.utc
        )
        mock_dt.strptime.side_effect = datetime.strptime
        datetime_now = mock_dt.now().isoformat(timespec="microseconds")

        tool_output = expenses.add_expense.invoke(
            {
                **base_expense,
                "telegram_user_id": TELEGRAM_USER_ID,
                "payment_method": "Card",
            }
        )

    assert tool_output == "Expense recorded."

    item = dynamodb.get_item(f"USER#{TELEGRAM_USER_ID}", f"EXPENSE#{datetime_now}")
    assert item == {
        **base_expense,
        "PK": f"USER#{TELEGRAM_USER_ID}",
        "SK": f"EXPENSE#{datetime_now}",
        "payment_method": "Card",
        "updated_at": datetime_now,
    }


def test_add_expense_default_payment_method(dynamodb_table, base_expense):
    with patch("src.bot.tools.expenses.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(
            2023, 12, 20, 13, 45, 50, 123456, tzinfo=timezone.utc
        )
        mock_dt.strptime.side_effect = datetime.strptime
        datetime_now = mock_dt.now().isoformat(timespec="microseconds")

        tool_output = expenses.add_expense.invoke(
            {**base_expense, "telegram_user_id": TELEGRAM_USER_ID}
        )

    assert tool_output == "Expense recorded."

    item = dynamodb.get_item(f"USER#{TELEGRAM_USER_ID}", f"EXPENSE#{datetime_now}")
    assert item == {
        **base_expense,
        "PK": f"USER#{TELEGRAM_USER_ID}",
        "SK": f"EXPENSE#{datetime_now}",
        "payment_method": "Cash",
        "updated_at": datetime_now,
    }


def test_add_expense_invalid_date_format(base_expense):
    tool_output = expenses.add_expense.invoke(
        {**base_expense, "telegram_user_id": TELEGRAM_USER_ID, "date": "2023-13-20"}
    )

    assert tool_output == "datetime should be in YYYY-MM-DD format (e.g. 2020-12-30)"


def test_add_expense_invalid_amount(base_expense):
    tool_output = expenses.add_expense.invoke(
        {
            **base_expense,
            "telegram_user_id": TELEGRAM_USER_ID,
            "amount": "invalid_amount",
        }
    )

    assert (
        tool_output
        == "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
    )


def test_add_expense_zero_amount(base_expense):
    tool_output = expenses.add_expense.invoke(
        {**base_expense, "telegram_user_id": TELEGRAM_USER_ID, "amount": "0"}
    )

    assert (
        tool_output
        == "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
    )


def test_add_expense_negative_amount(base_expense):
    tool_output = expenses.add_expense.invoke(
        {**base_expense, "telegram_user_id": TELEGRAM_USER_ID, "amount": "-1.35"}
    )

    assert (
        tool_output
        == "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
    )


def test_add_expense_invalid_category(base_expense):
    tool_output = expenses.add_expense.invoke(
        {**base_expense, "telegram_user_id": TELEGRAM_USER_ID, "category": "Casino"}
    )

    assert (
        tool_output
        == "category should be one of ['Accommodation', 'Car Rental', 'Flight', 'Food', 'Insurance', 'Leisure', 'Misc', 'Shopping', 'Transport']"
    )


def test_edit_expense_no_date_change_multiple_fields(dynamodb_table, base_expense):

    datetime_now = datetime(2023, 12, 20, 1, 2, 3, 0, tzinfo=timezone.utc).isoformat(
        timespec="microseconds"
    )

    pk = f"USER#{TELEGRAM_USER_ID}"
    sk = f"EXPENSE#{datetime_now}"

    dynamodb.put_item({**base_expense, "PK": pk, "SK": sk, "payment_method": "Card"})

    original_item = dynamodb.get_item(pk, sk)

    edit_fields = {
        "edit_message": "Edit expense number 1 to Shopping at Yakun $5.24 USD with cash",
        "summary": "Shopping at Yakun",
        "currency": "USD",
        "category": "Shopping",
        "amount": "5.24",
        "payment_method": "Cash",
    }

    with patch("src.bot.tools.expenses.datetime") as mock_dt:
        update_datetime = datetime(2025, 12, 13, 1, 2, 3, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = update_datetime

        tool_output = expenses.edit_expense.invoke(
            {"telegram_user_id": TELEGRAM_USER_ID, "expense_num": 1, **edit_fields}
        )

    edited_item = dynamodb.get_item(
        f"USER#{TELEGRAM_USER_ID}", f"EXPENSE#{datetime_now}"
    )

    assert edited_item == {
        **original_item,
        "source_message": f"{base_expense['source_message']} | {edit_fields['edit_message']}",
        "summary": edit_fields["summary"],
        "currency": edit_fields["currency"],
        "category": edit_fields["category"],
        "amount": edit_fields["amount"],
        "payment_method": edit_fields["payment_method"],
        "updated_at": update_datetime.isoformat(timespec="microseconds"),
    }

    assert tool_output == "Edit expense successful."


def test_edit_expense_date_change_earlier_date(dynamodb_table, base_expense):
    pk = f"USER#{TELEGRAM_USER_ID}"

    expense1_datetime = datetime(2026, 1, 15, 0, 0, 0, 1, tzinfo=timezone.utc)
    expense1_sk = f"EXPENSE#{expense1_datetime.isoformat(timespec='microseconds')}"
    dynamodb.put_item(
        {
            "PK": pk,
            "SK": expense1_sk,
            **base_expense,
            "date": expense1_datetime.strftime("%Y-%m-%d"),
        }
    )

    expense2_datetime = datetime(2026, 1, 16, 0, 0, 0, 1, tzinfo=timezone.utc)
    dynamodb.put_item(
        {
            "PK": pk,
            "SK": f"EXPENSE#{expense2_datetime.isoformat(timespec='microseconds')}",
            **base_expense,
            "date": expense2_datetime.strftime("%Y-%m-%d"),
        }
    )

    items_before_edit = dynamodb.query_by_prefix(pk, "EXPENSE#")

    new_date = "2026-01-14"
    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 1,
            "edit_message": "Change date of 1 to jan 14 2026",
            "summary": "Breakfast at Yakun on 2026-01-14 6.13 SGD",
            "date": new_date,
        }
    )
    assert tool_output == "Edit expense successful."

    items_after_edit = dynamodb.query_by_prefix(pk, "EXPENSE#")

    assert not dynamodb.get_item(pk, expense1_sk)
    assert items_after_edit[0].get("date") == new_date
    assert len(items_after_edit) == 2
    assert items_before_edit[1] == items_after_edit[1]


def test_edit_expense_date_change_reorders_list(dynamodb_table, base_expense):
    pk = f"USER#{TELEGRAM_USER_ID}"

    expense1_datetime = datetime(2026, 1, 15, 0, 0, 0, 1, tzinfo=timezone.utc)
    expense1_sk = f"EXPENSE#{expense1_datetime.isoformat(timespec='microseconds')}"
    dynamodb.put_item(
        {
            "PK": pk,
            "SK": expense1_sk,
            **base_expense,
            "date": expense1_datetime.strftime("%Y-%m-%d"),
        }
    )

    expense2_datetime = datetime(2026, 1, 16, 0, 0, 0, 1, tzinfo=timezone.utc)
    dynamodb.put_item(
        {
            "PK": pk,
            "SK": f"EXPENSE#{expense2_datetime.isoformat(timespec='microseconds')}",
            **base_expense,
            "date": expense2_datetime.strftime("%Y-%m-%d"),
        }
    )

    items_before_edit = dynamodb.query_by_prefix(pk, "EXPENSE#")

    new_date = "2026-01-17"
    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 1,
            "edit_message": "Change date of 1 to jan 17 2026",
            "summary": "Breakfast at Yakun on 2026-01-17 6.13 SGD",
            "date": new_date,
        }
    )
    assert tool_output == "Edit expense successful."

    items_after_edit = dynamodb.query_by_prefix(pk, "EXPENSE#")

    assert not dynamodb.get_item(pk, expense1_sk)
    assert len(items_after_edit) == 2
    assert items_before_edit[1] == items_after_edit[0]
    assert items_after_edit[1].get("date") == new_date


def test_edit_expense_no_optional_fields(dynamodb_table):
    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 1,
            "edit_message": "Unit test",
            "summary": "Unit test",
        }
    )

    assert (
        tool_output
        == "At least one of category, amount, currency, date, or payment_method must be provided."
    )


def test_edit_expense_no_items(dynamodb_table):
    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 1,
            "edit_message": "Unit test",
            "summary": "Unit test",
            "category": "Flight",
        }
    )

    assert (
        tool_output == "There are no items to edit. Add an expense to be tracked first."
    )


def test_edit_expense_expense_num_too_low(dynamodb_table, base_expense):
    dynamodb.put_item(
        {"PK": f"USER#{TELEGRAM_USER_ID}", "SK": "EXPENSE#1", **base_expense}
    )

    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": -1,
            "edit_message": "Unit test",
            "summary": "Unit test",
            "category": "Unit test",
        }
    )

    tool_output2 = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 0,
            "edit_message": "Unit test",
            "summary": "Unit test",
            "category": "Unit test",
        }
    )

    assert (
        tool_output
        == "Invalid expense number. Use the numbered list from get_all_expenses."
    )
    assert (
        tool_output2
        == "Invalid expense number. Use the numbered list from get_all_expenses."
    )


def test_edit_expense_expense_num_too_high(dynamodb_table, base_expense):
    dynamodb.put_item(
        {"PK": f"USER#{TELEGRAM_USER_ID}", "SK": "EXPENSE#1", **base_expense}
    )

    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 2,
            "edit_message": "Unit test",
            "summary": "Unit test",
            "category": "Flight",
        }
    )

    assert (
        tool_output
        == "Invalid expense number. Use the numbered list from get_all_expenses."
    )


def test_edit_expense_invalid_category(dynamodb_table, base_expense):
    dynamodb.put_item(
        {"PK": f"USER#{TELEGRAM_USER_ID}", "SK": "EXPENSE#1", **base_expense}
    )

    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 1,
            "edit_message": "Unit test",
            "summary": "Unit test",
            "category": "Unit test",
        }
    )

    assert (
        tool_output
        == "category should be one of ['Accommodation', 'Car Rental', 'Flight', 'Food', 'Insurance', 'Leisure', 'Misc', 'Shopping', 'Transport']"
    )


def test_edit_expense_zero_and_negative_amount(dynamodb_table, base_expense):
    dynamodb.put_item(
        {"PK": f"USER#{TELEGRAM_USER_ID}", "SK": "EXPENSE#1", **base_expense}
    )

    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 1,
            "edit_message": "Unit test",
            "summary": "Unit test",
            "amount": "0",
        }
    )

    tool_output2 = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 1,
            "edit_message": "Unit test",
            "summary": "Unit test",
            "amount": "-1",
        }
    )

    assert (
        tool_output
        == "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
    )
    assert (
        tool_output2
        == "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
    )


def test_edit_expense_non_numeric_amount(dynamodb_table, base_expense):
    dynamodb.put_item(
        {"PK": f"USER#{TELEGRAM_USER_ID}", "SK": "EXPENSE#1", **base_expense}
    )

    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 1,
            "edit_message": "Unit test",
            "summary": "Unit test",
            "amount": "Unit test",
        }
    )

    assert (
        tool_output
        == "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
    )


def test_edit_expense_invalid_date_format(dynamodb_table, base_expense):
    dynamodb.put_item(
        {"PK": f"USER#{TELEGRAM_USER_ID}", "SK": "EXPENSE#1", **base_expense}
    )

    tool_output = expenses.edit_expense.invoke(
        {
            "telegram_user_id": TELEGRAM_USER_ID,
            "expense_num": 1,
            "edit_message": "Unit test",
            "summary": "Unit test",
            "date": "Unit test",
        }
    )

    assert tool_output == "datetime should be in YYYY-MM-DD format (e.g. 2020-12-30)"


def test_delete_expense_deletes_single_item(dynamodb_table, base_expense):
    pk = f"USER#{TELEGRAM_USER_ID}"
    sk = "EXPENSE#1"

    dynamodb.put_item({"PK": pk, "SK": sk, **base_expense})

    tool_output = expenses.delete_expense.invoke(
        {"telegram_user_id": TELEGRAM_USER_ID, "expense_num": 1}
    )

    assert tool_output == "Expense deleted."
    assert dynamodb.get_item(pk, sk) is None
    assert len(dynamodb.query_by_prefix(pk, "EXPENSE#")) == 0


def test_delete_expense_deletes_correct_item_when_multiple_exist(
    dynamodb_table, base_expense
):
    pk = f"USER#{TELEGRAM_USER_ID}"
    item1 = {"PK": pk, "SK": "EXPENSE#1", **base_expense}
    dynamodb.put_item(item1)

    item2 = {"PK": pk, "SK": "EXPENSE#2", **base_expense}
    dynamodb.put_item(item2)

    tool_output = expenses.delete_expense.invoke(
        {"telegram_user_id": TELEGRAM_USER_ID, "expense_num": 1}
    )

    assert tool_output == "Expense deleted."
    assert dynamodb.get_item(pk, item1["SK"]) is None
    assert dynamodb.get_item(pk, item2["SK"]) == item2
    assert len(dynamodb.query_by_prefix(pk, "EXPENSE#")) == 1


def test_delete_expense_returns_error_when_expense_num_is_zero():
    tool_output = expenses.delete_expense.invoke(
        {"telegram_user_id": TELEGRAM_USER_ID, "expense_num": 0}
    )

    assert (
        tool_output
        == "Invalid expense number. Use the numbered list from get_all_expenses."
    )


def test_delete_expense_returns_error_when_expense_num_exceeds_list(
    dynamodb_table, base_expense
):
    dynamodb.put_item(
        {"PK": f"USER#{TELEGRAM_USER_ID}", "SK": "EXPENSE#1", **base_expense}
    )

    tool_output = expenses.delete_expense.invoke(
        {"telegram_user_id": TELEGRAM_USER_ID, "expense_num": 2}
    )

    assert (
        tool_output
        == "Invalid expense number. Use the numbered list from get_all_expenses."
    )


def test_delete_expense_returns_error_when_no_expenses_exist(dynamodb_table):
    tool_output = expenses.delete_expense.invoke(
        {"telegram_user_id": TELEGRAM_USER_ID, "expense_num": 1}
    )

    assert (
        tool_output
        == "There are no items to delete. Add an expense to be tracked first."
    )


def test_get_all_expenses_no_expenses(dynamodb_table):
    tool_output = expenses.get_all_expenses.invoke(
        {"telegram_user_id": TELEGRAM_USER_ID}
    )
    assert tool_output == "There is currently no expenses recorded."


def test_get_all_expenses_single_expense(dynamodb_table, base_expense):
    dynamodb.put_item(
        {
            **base_expense,
            "PK": f"USER#{TELEGRAM_USER_ID}",
            "SK": "EXPENSE#1",
            "payment_method": "Cash",
        }
    )

    expected_output = "summary | category | amount | date | payment_method"
    expected_output += "\n1. Breakfast at Yakun | Food | 6.13 SGD | 2023-12-20 | Cash"
    tool_output = expenses.get_all_expenses.invoke(
        {"telegram_user_id": TELEGRAM_USER_ID}
    )

    assert expected_output == tool_output


def test_get_all_expenses_multiple_expenses_ordered_by_insertion(
    dynamodb_table, base_expense
):
    dynamodb.put_item(
        {
            **base_expense,
            "PK": f"USER#{TELEGRAM_USER_ID}",
            "SK": "EXPENSE#2023-12-20T01:02:03.456789+00:00",
            "payment_method": "Cash",
        }
    )

    dynamodb.put_item(
        {
            "PK": f"USER#{TELEGRAM_USER_ID}",
            "SK": "EXPENSE#2023-01-15T12:34:56.789101+00:00",
            "source_message": "Jeans from Uniqlo $78.13",
            "summary": "Jeans from Uniqlo",
            "category": "Shopping",
            "amount": "345.67",
            "currency": "MYR",
            "date": "2023-01-25",
            "payment_method": "Card",
        }
    )

    expected_output = "summary | category | amount | date | payment_method"
    expected_output += (
        "\n1. Jeans from Uniqlo | Shopping | 345.67 MYR | 2023-01-25 | Card"
    )
    expected_output += "\n2. Breakfast at Yakun | Food | 6.13 SGD | 2023-12-20 | Cash"

    tool_output = expenses.get_all_expenses.invoke(
        {"telegram_user_id": TELEGRAM_USER_ID}
    )

    assert expected_output == tool_output
