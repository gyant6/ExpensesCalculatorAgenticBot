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


@tool
def add_expense(telegram_user_id: Annotated[str, InjectedState("telegram_user_id")], source_message: str, summary: str, category: str, amount: str, currency: str, date: str, payment_method: str = "Cash") -> str:
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
    
    try:
        amt = Decimal(amount)
        if amt <= 0: 
            return "amount should be greater than 0"
    except InvalidOperation:
        return "amount must be a valid positive number (e.g. '1200' or '12.50')"
    
    if category not in CATEGORIES:
        return f"category should be one of {sorted(CATEGORIES)}"
    
    datetime_now = datetime.now(timezone.utc).isoformat()
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


def edit_expense(telegram_user_id: Annotated[str, InjectedState("telegram_user_id")], ):
    # source_message: str, summary: str, category: str, amount: str, currency: str, date: str, payment_method: str = "Cash") -> str:
    raise NotImplementedError()


def delete_expense():
    raise NotImplementedError()


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
