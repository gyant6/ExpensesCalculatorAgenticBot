"""Tests for the expense tools.

Expenses are addressed by an immutable random id, with the current amount as a check.
The regression tests at the end reproduce the three ways a September 2026 trip
changed the wrong expense under positional addressing: a batch of date edits re-sorting
the list mid-batch, the model picking the neighbouring line, and every edit renaming its
target.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from src.bot.storage import dynamodb
from src.bot.tools import expenses

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

LEDGER = "123456"
PK = f"USER#{LEDGER}"
NOW = "2026-09-15T02:51:00.000000+00:00"
INVALID_AMOUNT = "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
INVALID_DATE = "datetime should be in YYYY-MM-DD format (e.g. 2020-12-30)"


def _seed(expense_id: str, **fields: Any) -> dict[str, Any]:
    """Store an expense under a given id, as add_expense would, and return it."""
    item: dict[str, Any] = {
        "PK": PK,
        "SK": f"{expenses.EXPENSE_SK_PREFIX}{expense_id}",
        "source_message": f"{fields.get('summary', 'Coffee')} message",
        "summary": "Coffee",
        "category": "Food",
        "amount": Decimal("6.13"),
        "currency": "SGD",
        "date": "2026-09-14",
        "payment_method": "Card",
        "created_at": NOW,
        "updated_at": NOW,
        **fields,
    }
    dynamodb.put_item(item)
    return item


def _get(expense_id: str) -> dict[str, Any] | None:
    return dynamodb.get_item(PK, f"{expenses.EXPENSE_SK_PREFIX}{expense_id}")


def _add(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "ledger_id": LEDGER,
        "message_date": "2026-01-30",
        "source_message": "Breakfast at Yakun $6.13",
        "summary": "Breakfast at Yakun",
        "category": "Food",
        "amount": "6.13",
        "currency": "SGD",
        **overrides,
    }
    return str(expenses.add_expense.invoke(payload))


def _edit(expense_id: str, expected_amount: str, **changes: Any) -> str:
    payload: dict[str, Any] = {
        "ledger_id": LEDGER,
        "expense_id": expense_id,
        "expected_amount": expected_amount,
        "edit_message": "edit",
        **changes,
    }
    return str(expenses.edit_expense.invoke(payload))


def _delete(expense_id: str, expected_amount: str) -> str:
    return str(
        expenses.delete_expense.invoke(
            {
                "ledger_id": LEDGER,
                "expense_id": expense_id,
                "expected_amount": expected_amount,
            }
        )
    )


# ── id generation ────────────────────────────────────────────────────────────


def test_generated_ids_use_only_the_unambiguous_alphabet() -> None:
    for _ in range(500):
        expense_id = expenses._new_expense_id()
        assert len(expense_id) == 4
        assert set(expense_id) <= set(expenses._ID_ALPHABET)
    assert not set("0o1li") & set(expenses._ID_ALPHABET)


# ── add_expense ──────────────────────────────────────────────────────────────


def test_add_expense_records_under_its_id(dynamodb_table: DynamoDBClient) -> None:
    with (
        patch.object(expenses, "_new_expense_id", return_value="k7qm"),
        patch.object(expenses, "_now_utc", return_value=NOW),
    ):
        output = _add(payment_method="Card")

    assert output == "Expense recorded with id k7qm."
    assert _get("k7qm") == {
        "PK": PK,
        "SK": "EXPENSE#k7qm",
        "source_message": "Breakfast at Yakun $6.13",
        "summary": "Breakfast at Yakun",
        "category": "Food",
        "amount": Decimal("6.13"),
        "currency": "SGD",
        "date": "2026-01-30",
        "payment_method": "Card",
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_add_expense_defaults_payment_method_to_cash(
    dynamodb_table: DynamoDBClient,
) -> None:
    with patch.object(expenses, "_new_expense_id", return_value="k7qm"):
        _add()

    item = _get("k7qm")
    assert item is not None
    assert item["payment_method"] == "Cash"


def test_add_expense_without_a_date_uses_the_message_date(
    dynamodb_table: DynamoDBClient,
) -> None:
    with patch.object(expenses, "_new_expense_id", return_value="k7qm"):
        _add(date=None)

    item = _get("k7qm")
    assert item is not None
    assert item["date"] == "2026-01-30"


def test_add_expense_retries_a_colliding_id_without_overwriting(
    dynamodb_table: DynamoDBClient,
) -> None:
    existing = _seed("k7qm", summary="Curry", amount=Decimal("29.38"))

    with patch.object(expenses, "_new_expense_id", side_effect=["k7qm", "x9zz"]):
        output = _add()

    assert output == "Expense recorded with id x9zz."
    assert _get("k7qm") == existing


def test_add_expense_gives_up_after_repeated_collisions(
    dynamodb_table: DynamoDBClient,
) -> None:
    _seed("k7qm")

    with patch.object(expenses, "_new_expense_id", return_value="k7qm"):
        with pytest.raises(RuntimeError, match="unused expense id"):
            _add()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"date": "2023-13-20"}, INVALID_DATE),
        ({"amount": "invalid_amount"}, INVALID_AMOUNT),
        ({"amount": "0"}, INVALID_AMOUNT),
        ({"amount": "-1.35"}, INVALID_AMOUNT),
        (
            {"category": "Casino"},
            "category should be one of ['Accommodation', 'Car Rental', 'Flight', "
            "'Food', 'Insurance', 'Leisure', 'Misc', 'Shopping', 'Transport']",
        ),
    ],
)
def test_add_expense_rejects_invalid_input(
    dynamodb_table: DynamoDBClient, overrides: dict[str, Any], message: str
) -> None:
    assert _add(**overrides) == message
    assert dynamodb.query_by_prefix(PK, expenses.EXPENSE_SK_PREFIX) == []


# ── edit_expense ─────────────────────────────────────────────────────────────


def test_edit_updates_fields_and_keeps_the_name(
    dynamodb_table: DynamoDBClient,
) -> None:
    original = _seed("k7qm", summary="Curry", amount=Decimal("29.38"))

    with patch.object(expenses, "_now_utc", return_value="2026-09-27T00:00:00+00:00"):
        output = _edit(
            "k7qm",
            "29.38",
            edit_message="it was 32.19 by cash",
            amount="32.19",
            payment_method="Cash",
        )

    assert output == "Edit expense successful."
    assert _get("k7qm") == {
        **original,
        "amount": Decimal("32.19"),
        "payment_method": "Cash",
        "source_message": "Curry message | it was 32.19 by cash",
        "updated_at": "2026-09-27T00:00:00+00:00",
    }


def test_edit_renames_only_when_a_summary_is_given(
    dynamodb_table: DynamoDBClient,
) -> None:
    _seed("k7qm", summary="Clothes", amount=Decimal("4.9"))

    _edit("k7qm", "4.9", summary="Clothes shopping")

    item = _get("k7qm")
    assert item is not None
    assert item["summary"] == "Clothes shopping"


def test_date_edit_updates_in_place(dynamodb_table: DynamoDBClient) -> None:
    _seed("k7qm", date="2026-09-15")

    _edit("k7qm", "6.13", date="2026-09-13")

    items = dynamodb.query_by_prefix(PK, expenses.EXPENSE_SK_PREFIX)
    assert [(i["SK"], i["date"]) for i in items] == [("EXPENSE#k7qm", "2026-09-13")]


def test_expected_amount_compares_numerically(dynamodb_table: DynamoDBClient) -> None:
    _seed("k7qm", amount=Decimal("29.38"))

    assert _edit("k7qm", "29.380", category="Shopping") == "Edit expense successful."


def test_edit_is_refused_when_the_amount_does_not_match(
    dynamodb_table: DynamoDBClient,
) -> None:
    original = _seed("k7qm", summary="poke bowl", amount=Decimal("30.7"))

    output = _edit("k7qm", "29.38", amount="32.19")

    assert (
        "poke bowl" in output and "30.7" in output and "Nothing was changed" in output
    )
    assert _get("k7qm") == original


def test_edit_of_an_unknown_id_is_refused(dynamodb_table: DynamoDBClient) -> None:
    output = _edit("zzzz", "6.13", category="Shopping")

    assert "No expense has id 'zzzz'" in output
    assert dynamodb.query_by_prefix(PK, expenses.EXPENSE_SK_PREFIX) == []


def test_edit_with_an_invalid_expected_amount_is_refused(
    dynamodb_table: DynamoDBClient,
) -> None:
    original = _seed("k7qm")

    assert "expected_amount" in _edit("k7qm", "about six", category="Shopping")
    assert _get("k7qm") == original


def test_edit_with_nothing_to_change_is_refused(
    dynamodb_table: DynamoDBClient,
) -> None:
    _seed("k7qm")

    assert _edit("k7qm", "6.13") == (
        "At least one of summary, category, amount, currency, date, or "
        "payment_method must be provided."
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"category": "Casino"},
            "category should be one of ['Accommodation', 'Car Rental', 'Flight', "
            "'Food', 'Insurance', 'Leisure', 'Misc', 'Shopping', 'Transport']",
        ),
        ({"amount": "0"}, INVALID_AMOUNT),
        ({"amount": "-1"}, INVALID_AMOUNT),
        ({"amount": "lots"}, INVALID_AMOUNT),
        ({"date": "14 Sep"}, INVALID_DATE),
    ],
)
def test_edit_rejects_invalid_values(
    dynamodb_table: DynamoDBClient, changes: dict[str, Any], message: str
) -> None:
    original = _seed("k7qm")

    assert _edit("k7qm", "6.13", **changes) == message
    assert _get("k7qm") == original


# ── delete_expense ───────────────────────────────────────────────────────────


def test_delete_removes_only_the_target(dynamodb_table: DynamoDBClient) -> None:
    _seed("k7qm", summary="Clothes", amount=Decimal("4.9"))
    other = _seed("x9zz", summary="Teacup", amount=Decimal("19.08"))

    assert _delete("k7qm", "4.9") == "Expense deleted."
    assert _get("k7qm") is None
    assert _get("x9zz") == other


def test_delete_is_refused_when_the_amount_does_not_match(
    dynamodb_table: DynamoDBClient,
) -> None:
    original = _seed("k7qm", summary="Teacup", amount=Decimal("19.08"))

    assert "Nothing was changed" in _delete("k7qm", "4.9")
    assert _get("k7qm") == original


def test_delete_of_an_unknown_id_is_refused(dynamodb_table: DynamoDBClient) -> None:
    assert "No expense has id 'zzzz'" in _delete("zzzz", "6.13")


# ── get_all_expenses ─────────────────────────────────────────────────────────


def test_get_all_expenses_with_none_recorded(dynamodb_table: DynamoDBClient) -> None:
    output = expenses.get_all_expenses.invoke({"ledger_id": LEDGER})

    assert output == "There are currently no expenses recorded."


def test_get_all_expenses_lists_ids_for_the_model(
    dynamodb_table: DynamoDBClient,
) -> None:
    _seed("k7qm", summary="Breakfast at Yakun", payment_method="Cash")

    output = expenses.get_all_expenses.invoke({"ledger_id": LEDGER})

    assert output == (
        "number | id | summary | category | amount | date | payment_method\n"
        "1 | k7qm | Breakfast at Yakun | Food | 6.13 SGD | 2026-09-14 | Cash"
    )


def test_list_is_sorted_by_date_then_creation_time(
    dynamodb_table: DynamoDBClient,
) -> None:
    # Ids sort in the opposite order to the dates, so key order cannot pass this.
    _seed("aaaa", summary="Third", date="2026-09-14", created_at="2026-09-14T09:00Z")
    _seed("bbbb", summary="Second", date="2026-09-13", created_at="2026-09-15T09:00Z")
    _seed("cccc", summary="First", date="2026-09-13", created_at="2026-09-13T09:00Z")

    assert [e["summary"] for e in expenses.list_expenses(LEDGER)] == [
        "First",
        "Second",
        "Third",
    ]


# ── September trip regressions ──────────────────────────────────────────────────


def test_batch_of_date_edits_changes_exactly_its_targets(
    dynamodb_table: DynamoDBClient,
) -> None:
    """15 Sep: "change 13-15 to 13 Sep", then "change 9-12 to 12 Sep".

    Each date edit used to rewrite the sort key and re-sort the list, so later numbers
    in the batch pointed at other expenses, which were then renamed. With ids taken from
    one listing, every edit lands on its intended expense whatever the list order does.
    """
    _seed(
        "aaaa",
        summary="Fridge magnet",
        amount=Decimal("4.95"),
        date="2026-09-13",
    )
    _seed("bbbb", summary="Postcards", amount=Decimal("27.86"), date="2026-09-13")
    _seed("cccc", summary="Bakery", amount=Decimal("21.04"), date="2026-09-13")
    _seed("dddd", summary="Farm snacks", amount=Decimal("34.84"), date="2026-09-14")
    _seed("eeee", summary="Transit top up", amount=Decimal("7.62"), date="2026-09-14")
    untouched = _seed(
        "ffff", summary="Coffee", amount=Decimal("6.13"), date="2026-09-14"
    )

    for expense_id, amount in [("cccc", "21.04"), ("dddd", "34.84"), ("eeee", "7.62")]:
        assert (
            _edit(expense_id, amount, date="2026-09-13") == "Edit expense successful."
        )
    for expense_id, amount in [("aaaa", "4.95"), ("bbbb", "27.86")]:
        assert (
            _edit(expense_id, amount, date="2026-09-12") == "Edit expense successful."
        )

    by_name = {e["summary"]: e for e in expenses.list_expenses(LEDGER)}
    assert {name: e["date"] for name, e in by_name.items()} == {
        "Fridge magnet": "2026-09-12",
        "Postcards": "2026-09-12",
        "Bakery": "2026-09-13",
        "Farm snacks": "2026-09-13",
        "Transit top up": "2026-09-13",
        "Coffee": "2026-09-14",
    }
    assert {name: e["amount"] for name, e in by_name.items()}[
        "Transit top up"
    ] == Decimal("7.62")
    assert _get("ffff") == untouched


def test_neighbouring_line_is_caught_by_the_amount(
    dynamodb_table: DynamoDBClient,
) -> None:
    """26 Sep: "edit the curry to 32.19" edited the poke bowl beside it.

    An id copied from the wrong line still names a real expense, so ids alone would not
    catch it; the amount does.
    """
    poke = _seed("p0ke", summary="poke bowl", amount=Decimal("30.7"))
    curry = _seed("k7qm", summary="Curry", amount=Decimal("29.38"))

    assert "Nothing was changed" in _edit("p0ke", "29.38", amount="32.19")
    assert _get("p0ke") == poke

    assert _edit("k7qm", "29.38", amount="32.19") == "Edit expense successful."
    edited = _get("k7qm")
    assert edited is not None
    assert edited["amount"] == Decimal("32.19")
    assert edited["summary"] == curry["summary"]


def test_an_edit_that_names_nothing_new_cannot_relabel(
    dynamodb_table: DynamoDBClient,
) -> None:
    """18 Sep: "change to shopping" renamed the Teacup "Clothes".

    The model had to pass a summary with every edit. A category change now leaves the
    name alone, so even a misdirected edit cannot disguise itself as a duplicate.
    """
    _seed("t3cp", summary="Teacup", amount=Decimal("19.08"), category="Shopping")

    _edit("t3cp", "19.08", category="Leisure")

    item = _get("t3cp")
    assert item is not None
    assert item["summary"] == "Teacup"
