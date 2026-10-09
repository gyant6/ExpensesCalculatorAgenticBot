"""LangChain tools for recording, editing, deleting, and listing travel expenses.

Each expense is stored under a short random ID that never changes, `EXPENSE#<id>`, and
edits and deletes address it by that ID — never by its position in a list. Positions
were unstable: a date edit re-sorted the list, so later numbers in the same batch pointed
at different expenses, and the model sometimes picked the line next to the one it meant.
As a second guard, edits and deletes also state the expense's current amount and are
refused when it does not match, which catches an ID copied from the neighbouring line.
"""

import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Final

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from src.bot.storage import dynamodb

CATEGORIES = {
    "Food",
    "Car Rental",
    "Transport",
    "Accommodation",
    "Shopping",
    "Flight",
    "Insurance",
    "Leisure",
    "Misc",
}

EXPENSE_SK_PREFIX: Final = "EXPENSE#"

# Lowercase letters and digits without the look-alikes 0/o, 1/l/i, so an ID survives
# being read and retyped. Four characters give 31**4 ≈ 923,000 IDs per ledger.
_ID_ALPHABET: Final = "23456789abcdefghjkmnpqrstuvwxyz"
_ID_LENGTH: Final = 4

# A collision needs the same 4-character ID twice within one ledger, so a retry is
# already rare; five consecutive collisions mean something other than chance is wrong.
_MAX_ID_ATTEMPTS: Final = 5

_INVALID_AMOUNT = "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
_INVALID_DATE = "datetime should be in YYYY-MM-DD format (e.g. 2020-12-30)"


def check_valid_amount(amount: str) -> bool:
    """Validate that an amount string represents a positive number.

    Args:
        amount: The amount string to validate (e.g. '12.50').

    Returns:
        True if the amount is a valid positive number, False otherwise.
    """
    try:
        return Decimal(amount) > 0
    except InvalidOperation:
        return False


def _is_valid_date(date: str) -> bool:
    """Return True if the string is a calendar date in YYYY-MM-DD form."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _new_expense_id() -> str:
    """Generate a random expense ID from the unambiguous alphabet."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))


def _now_utc() -> str:
    """Current UTC time as an ISO-8601 timestamp with microseconds."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def list_expenses(ledger_id: str) -> list[dict[str, Any]]:
    """Return every expense in a ledger, in the order the user should see them.

    Sorted by the date the expense happened, then by when it was recorded, so the
    numbering is chronological. The sort key is a random ID and carries no order.

    Args:
        ledger_id: The ledger (Telegram chat ID) whose expenses to list.

    Returns:
        The expense items as plain dicts, oldest first. Empty if there are none.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    items = dynamodb.query_by_prefix(f"USER#{ledger_id}", EXPENSE_SK_PREFIX)
    return sorted(items, key=lambda item: (item["date"], item.get("created_at", "")))


def _expense_id(item: dict[str, Any]) -> str:
    """The ID part of an expense's sort key."""
    return str(item["SK"]).removeprefix(EXPENSE_SK_PREFIX)


def _load_for_change(
    ledger_id: str, expense_id: str, expected_amount: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch the expense an edit or delete targets, and check it is the one meant.

    Args:
        ledger_id: The ledger the expense belongs to.
        expense_id: The ID the model supplied.
        expected_amount: The amount the model believes the expense currently has.

    Returns:
        (item, None) when the expense exists and its amount matches, or (None, message)
        with an error the model can act on.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    if not check_valid_amount(expected_amount):
        return None, (
            "expected_amount must be the expense's current amount as a positive number."
        )

    item = dynamodb.get_item(f"USER#{ledger_id}", f"{EXPENSE_SK_PREFIX}{expense_id}")
    if item is None:
        return None, (
            f"No expense has id {expense_id!r}. Call get_all_expenses and use an id "
            "from its result."
        )

    actual = Decimal(str(item["amount"]))
    if actual != Decimal(expected_amount):
        return None, (
            f"Expense {expense_id} is '{item['summary']}', {item['amount']} "
            f"{item['currency']} — not {expected_amount}. That may be the wrong expense. "
            "Nothing was changed: call get_all_expenses and retry with the right id."
        )
    return item, None


@tool
def add_expense(
    ledger_id: Annotated[str, InjectedState("ledger_id")],
    message_date: Annotated[str, InjectedState("message_date")],
    source_message: str,
    summary: str,
    category: str,
    amount: str,
    currency: str,
    date: str | None = None,
    payment_method: str = "Cash",
) -> str:
    """Record a new expense for the user in DynamoDB.

    Call this when the user describes an expense — e.g. "spent $12 on lunch", "paid 500 yen
    for dinner", "bought a train ticket for $3.20". Extract date from the user's message if
    explicitly mentioned (e.g. "on Tuesday", "14 June"); otherwise fall back to the Telegram
    message date available in agent state. Do not guess or use today's date as a default.

    The result includes the new expense's id. Keep it for a follow-up edit in the same
    conversation (e.g. "it was by card"), but never show it to the user.

    Args:
        source_message: The original message the user sent describing the expense.
        summary: Short human-readable description of the expense (e.g. 'Lunch at Sushi Tei').
        category: Expense category which must be in one of
            "Food", "Car Rental", "Transport", "Accommodation",
            "Shopping", "Flight", "Insurance", "Leisure", "Misc".
        amount: Expense amount as a string (e.g. '12.50'). Must be a positive number.
        currency: ISO 4217 currency code (e.g. 'SGD', 'JPY', 'USD').
        date: Date the expense occurred in YYYY-MM-DD format (e.g. '2026-06-14'). Use the
            date explicitly mentioned by the user or None if the user does not specify it.
        payment_method: How the expense was paid (e.g. 'Cash', 'Card', 'PayNow').
            If the payment method is not mentioned, fall back to 'Cash'.

    Returns:
        A confirmation string with the expense's id, or an error string describing what
        was invalid.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
        RuntimeError: If no unused id could be generated, which chance alone does not
            produce.
    """
    if date is None:
        date = message_date
    if not _is_valid_date(date):
        return _INVALID_DATE
    if not check_valid_amount(amount):
        return _INVALID_AMOUNT
    if category not in CATEGORIES:
        return f"category should be one of {sorted(CATEGORIES)}"

    now = _now_utc()
    for _ in range(_MAX_ID_ATTEMPTS):
        expense_id = _new_expense_id()
        written = dynamodb.put_item_if_absent(
            {
                "PK": f"USER#{ledger_id}",
                "SK": f"{EXPENSE_SK_PREFIX}{expense_id}",
                "source_message": source_message,
                "summary": summary,
                "category": category,
                "amount": Decimal(amount),
                "currency": currency,
                "date": date,
                "payment_method": payment_method,
                "created_at": now,
                "updated_at": now,
            }
        )
        if written:
            return f"Expense recorded with id {expense_id}."
    raise RuntimeError(
        f"Could not find an unused expense id in {_MAX_ID_ATTEMPTS} attempts for "
        f"ledger {ledger_id}; the id generator is not producing random values."
    )


@tool
def edit_expense(
    ledger_id: Annotated[str, InjectedState("ledger_id")],
    expense_id: str,
    expected_amount: str,
    edit_message: str,
    summary: str | None = None,
    category: str | None = None,
    amount: str | None = None,
    currency: str | None = None,
    date: str | None = None,
    payment_method: str | None = None,
) -> str:
    """Edit one or more fields of an existing expense.

    Call this when the user wants to correct or update a previously recorded expense —
    e.g. "change the curry to 32.19", "the ramen was actually by card", "move the
    taxi to 14 Sep".

    Identify the expense by its id from the most recent get_all_expenses or add_expense
    result in this conversation — never by its position in a list. If you do not have a
    current id, call get_all_expenses first. Pass the expense's current amount as
    expected_amount; the edit is refused if it does not match, which means you have the
    wrong expense and should look again rather than retry the same id.

    Only pass summary when the user asks to rename the expense. Other edits leave the
    name as it is.

    Args:
        expense_id: The expense's id, as given by get_all_expenses or add_expense.
        expected_amount: The expense's amount before this edit, e.g. '29.38'.
        edit_message: The user's message describing the edit. Appended to the original
            source_message.
        summary: New short description, only if the user asked to rename it.
        category: New category. Must be one of the valid categories if provided.
        amount: New amount as a string. Must be a positive number if provided.
        currency: New ISO 4217 currency code if provided.
        date: New date in YYYY-MM-DD format if provided.
        payment_method: New payment method if provided.

    Returns:
        A confirmation string on success, or an error string if nothing was to change,
        the id is unknown, the amount did not match, or a field value failed validation.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails, including when
            the expense is deleted between being read and being updated.
    """
    changes: dict[str, Any] = {}
    if summary:
        changes["summary"] = summary
    if category:
        if category not in CATEGORIES:
            return f"category should be one of {sorted(CATEGORIES)}"
        changes["category"] = category
    if amount:
        if not check_valid_amount(amount):
            return _INVALID_AMOUNT
        changes["amount"] = Decimal(amount)
    if currency:
        changes["currency"] = currency
    if date:
        if not _is_valid_date(date):
            return _INVALID_DATE
        changes["date"] = date
    if payment_method:
        changes["payment_method"] = payment_method
    if not changes:
        return (
            "At least one of summary, category, amount, currency, date, or "
            "payment_method must be provided."
        )

    item, error = _load_for_change(ledger_id, expense_id, expected_amount)
    if item is None:
        return error or "The expense could not be loaded."

    # A date edit is an ordinary update: the key is the id, not the date, so nothing
    # moves and no other expense's position changes.
    dynamodb.update_item(
        item["PK"],
        item["SK"],
        {
            **changes,
            "source_message": f"{item['source_message']} | {edit_message}",
            "updated_at": _now_utc(),
        },
    )
    return "Edit expense successful."


@tool
def delete_expense(
    ledger_id: Annotated[str, InjectedState("ledger_id")],
    expense_id: str,
    expected_amount: str,
) -> str:
    """Delete an existing expense.

    Call this when the user wants to remove a previously recorded expense —
    e.g. "delete the taxi", "remove the duplicate coffee", "that entry was a mistake".

    Identify the expense by its id from the most recent get_all_expenses or add_expense
    result — never by its position in a list. Pass its current amount as
    expected_amount; the delete is refused if it does not match.

    Args:
        expense_id: The expense's id, as given by get_all_expenses or add_expense.
        expected_amount: The expense's current amount, e.g. '29.38'.

    Returns:
        A confirmation string on success, or an error string if the id is unknown or the
        amount did not match.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    item, error = _load_for_change(ledger_id, expense_id, expected_amount)
    if item is None:
        return error or "The expense could not be loaded."

    dynamodb.delete_item(item["PK"], item["SK"])
    return "Expense deleted."


@tool
def get_all_expenses(
    ledger_id: Annotated[str, InjectedState("ledger_id")],
) -> str:
    """Retrieve all recorded expenses for the user.

    Call this when the user asks to see their expenses, wants a summary of spending,
    asks questions like "what have I spent so far" or "show me my expenses", and before
    editing or deleting an expense whose id you do not have from this turn.

    The id column is for edit_expense and delete_expense only. Never show ids to the
    user: when listing expenses for them, number the lines and omit the id.

    Returns:
        A list of expenses as a string, one per line, oldest first, with columns:
        number, id, summary, category, amount and currency, date, payment_method.
        Returns a message indicating no expenses if none are recorded.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    items = list_expenses(ledger_id)
    if not items:
        return "There are currently no expenses recorded."

    lines = ["number | id | summary | category | amount | date | payment_method"]
    for number, expense in enumerate(items, start=1):
        lines.append(
            f"{number} | {_expense_id(expense)} | {expense['summary']} | "
            f"{expense['category']} | {expense['amount']} {expense['currency']} | "
            f"{expense['date']} | {expense['payment_method']}"
        )
    return "\n".join(lines)
