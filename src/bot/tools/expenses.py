from src.bot.storage import dynamodb

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from typing import Annotated

CATEGORIES = {
    "Food", "Car Rental", "Transport", "Accommodation", 
    "Shopping", "Flight", "Insurance", "Leisure", "Misc"
}


def check_valid_amount(amount: str) -> bool:
    """Validate that an amount string represents a positive number.

    Args:
        amount: The amount string to validate (e.g. '12.50').

    Returns:
        True if the amount is a valid positive number, False otherwise.
    """
    try:
        amt = Decimal(amount)
        if amt <= 0: 
            return False
        return True
    except InvalidOperation:
        return False


@tool
def add_expense(telegram_user_id: Annotated[str, InjectedState("telegram_user_id")], source_message: str, summary: str, category: str, 
                amount: str, currency: str, date: str, payment_method: str = "Cash") -> str:
    """Record a new expense for the user in DynamoDB.

    Call this when the user describes an expense — e.g. "spent $12 on lunch", "paid 500 yen
    for dinner", "bought a train ticket for $3.20". Extract date from the user's message if
    explicitly mentioned (e.g. "on Tuesday", "14 June"); otherwise fall back to the Telegram
    message date available in agent state. Do not guess or use today's date as a default.

    Args:
        source_message: The original message the user sent describing the expense.
        summary: Short human-readable description of the expense (e.g. 'Lunch at Sushi Tei').
        category: Expense category which must be in one of 
            "Food", "Car Rental", "Transport", "Accommodation", 
            "Shopping", "Flight", "Insurance", "Leisure", "Misc".
        amount: Expense amount as a string (e.g. '12.50'). Must be a positive number.
        currency: ISO 4217 currency code (e.g. 'SGD', 'JPY', 'USD').
        date: Date the expense occurred in YYYY-MM-DD format (e.g. '2026-06-14'). Use the
            date explicitly mentioned by the user, or fall back to the Telegram message date
            from agent state if none is mentioned.
        payment_method: How the expense was paid (e.g. 'Cash', 'Card', 'PayNow').
            If the payment method is not mentioned, fall back to 'Cash'.

    Returns:
        A confirmation string on success, or an error string describing what was invalid.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return "datetime should be in YYYY-MM-DD format (e.g. 2020-12-30)"
    
    if not check_valid_amount(amount):
        return "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
    
    if category not in CATEGORIES:
        return f"category should be one of {sorted(CATEGORIES)}"
    
    datetime_now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    dynamodb.put_item({
        "PK": f"USER#{telegram_user_id}",
        "SK": f"EXPENSE#{datetime_now}",
        "source_message": source_message,
        "summary": summary,
        "category": category,
        "amount": amount,
        "currency": currency,
        "date": date,
        "payment_method": payment_method,
        "updated_at": datetime_now
    })
    
    return "Expense recorded."


@tool
def edit_expense(telegram_user_id: Annotated[str, InjectedState("telegram_user_id")], expense_num: int, edit_message: str,
                 summary: str, category: str = None, amount: str = None, currency: str = None, date: str = None, payment_method: str = None) -> str:
    """Edit one or more fields of an existing expense.

    Call this when the user wants to correct or update a previously recorded expense —
    e.g. "change expense 2 to USD", "the amount for expense 1 was actually 15.50",
    "I paid by card not cash for expense 3".

    The user's edit message is appended to the original source_message to preserve
    context. Generate a new summary that reflects the updated fields — do not reuse
    the old summary verbatim if the edit changes its meaning.

    When date is changed, the expense SK is regenerated so the list order reflects
    the new date. All other edits update the existing item in place.

    Args:
        expense_num: 1-based index of the expense to edit, as shown by get_all_expenses.
        edit_message: The user's message describing the edit. Appended to the original source_message.
        summary: New short description reflecting the updated expense.
        category: New category. Must be one of the valid categories if provided.
        amount: New amount as a string. Must be a positive number if provided.
        currency: New ISO 4217 currency code if provided.
        date: New date in YYYY-MM-DD format. Triggers an SK update if provided.
        payment_method: New payment method if provided.

    Returns:
        A confirmation string on success, or an error string if no optional fields were
        provided, the expense number is invalid, or a field value failed validation.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """

    if not category and not amount and not currency and not date and not payment_method:
        return "At least one of category, amount, currency, date, or payment_method must be provided."
    
    invalid_expense_str = "Invalid expense number. Use the numbered list from get_all_expenses."
    if expense_num < 1:
        return invalid_expense_str
                
    edited_fields = { "summary": summary }

    if category:
        if category not in CATEGORIES:
            return f"category should be one of {sorted(CATEGORIES)}"
        edited_fields["category"] = category
        
    if amount:
        if not check_valid_amount(amount):
            return "amount must be a valid positive number (e.g. '1200' or '12.50') and should not be 0."
        else:
            edited_fields["amount"] = amount
            
    if currency:
        edited_fields["currency"] = currency

    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
            
        except ValueError:
            return "datetime should be in YYYY-MM-DD format (e.g. 2020-12-30)"

    if payment_method:
        edited_fields["payment_method"] = payment_method

    items = dynamodb.query_by_prefix(f"USER#{telegram_user_id}", "EXPENSE#")
    if not items:
        return "There are no items to edit. Add an expense to be tracked first."
    if expense_num > len(items):
        return invalid_expense_str
    
    current_item = items[expense_num-1]
    edited_fields["source_message"] = f"{current_item["source_message"]} | {edit_message}"
        
    update_datetime = datetime.now(timezone.utc)
    if not date:
        dynamodb.update_item(
            current_item["PK"],
            current_item["SK"],
            {"updated_at": update_datetime.isoformat(timespec='microseconds'), **edited_fields}
        )
    else:
        edited_fields["date"] = date
        new_date = date + update_datetime.isoformat(timespec='microseconds')[10:]  # append THH:MM:SS.ffffff+HH:MM from edit timestamp for uniqueness

        dynamodb.transact_write_delete_put(
            current_item["PK"], 
            current_item["SK"],
            {
                **current_item,
                "SK": f"EXPENSE#{new_date}",
                "updated_at": update_datetime.isoformat(timespec='microseconds'),
                **edited_fields
            }
        )

    return "Edit expense successful."


@tool
def delete_expense(telegram_user_id: Annotated[str, InjectedState("telegram_user_id")], expense_num: int) -> str:
    """Delete an existing expense by its list position.

    Call this when the user wants to remove a previously recorded expense —
    e.g. "delete expense 2", "remove the third expense", "that entry was a mistake".

    Args:
        expense_num: 1-based index of the expense to delete, as shown by get_all_expenses.

    Returns:
        A confirmation string on success, or an error string if the expense number is
        invalid or no expenses exist.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    invalid_expense_str = "Invalid expense number. Use the numbered list from get_all_expenses."
    if expense_num < 1:
        return invalid_expense_str

    pk = f"USER#{telegram_user_id}"
    items = dynamodb.query_by_prefix(pk, "EXPENSE#")
    if not items:
        return "There are no items to delete. Add an expense to be tracked first."
    if expense_num > len(items):
        return invalid_expense_str
    
    item = items[expense_num-1]
    dynamodb.delete_item(pk, item["SK"])
    return "Expense deleted."


@tool
def get_all_expenses(telegram_user_id: Annotated[str, InjectedState("telegram_user_id")]) -> str:
    """Retrieve all recorded expenses for the user.

    Call this when the user asks to see their expenses, wants a summary of spending,
    or asks questions like "what have I spent so far" or "show me my expenses".

    Returns:
        A formatted list of expenses as a string, one per line, with columns:
        index, summary, category, amount, currency, date, payment_method.
        Returns a message indicating no expenses if none are recorded.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    items = dynamodb.query_by_prefix(f"USER#{telegram_user_id}", "EXPENSE#")
    
    if not items:
        return "There is currently no expenses recorded."
    
    response = "summary | category | amount | date | payment_method"
    for i, expense in enumerate(items, start=1):
        response += f"\n{i}. {expense["summary"]} | {expense["category"]} | {expense["amount"]} {expense["currency"]} | {expense["date"]} | {expense["payment_method"]}"
        
    return response
