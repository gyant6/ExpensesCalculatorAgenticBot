"""Fixed conversations for comparing models, each with the tool calls a correct reply makes.

Each scenario is a single model call: the conversation so far, the trip context the bot
would supply, and the tool calls the model's first response should contain. Only that
first response is checked. The tools never run, so nothing reads or writes DynamoDB.

The situations are the ones that went wrong, or could, on real trips: relative dates,
the "$" means SGD rule, several expenses in one message, edits that must name the right
id with its current amount, and the end-trip sequence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

# A trip in Seoul, started on a Monday; "today" is the Friday of the same week.
TRIP_START: Final = "2026-10-05"
TODAY: Final = "2026-10-09"
YESTERDAY: Final = "2026-10-08"
LAST_TUESDAY: Final = "2026-10-06"
TRIP_ZONE: Final = "Asia/Seoul"

# An expected argument is one accepted value, or a tuple of them. None in a tuple means
# the argument may also be left out — e.g. a date the tool defaults to today anyway.
Accepted = str | tuple[str | None, ...]

# The tools' own defaults, accepted whether the model passes them or leaves them out.
_TODAY_OR_DEFAULT: Final[Accepted] = (TODAY, None)
_CARD_OR_DEFAULT: Final[Accepted] = ("Card", None)


@dataclass(frozen=True)
class ExpectedCall:
    """One tool call a correct response contains.

    Attributes:
        tool: The tool's name.
        args: The arguments checked, by name. Arguments not listed are not checked.
    """

    tool: str
    args: Mapping[str, Accepted] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    """One conversation and the tool calls the model's next response should make.

    Attributes:
        name: Short identifier, used to select scenarios on the command line.
        description: What the scenario tests.
        messages: The conversation so far, ending with the user's message.
        expected_calls: Every tool call a correct response makes, in any order. Empty
            when the correct response is a text reply with no tool call.
        trip_start_date: The active trip's start date, or None for no trip.
        local_date: Today's date in the trip's zone, as check_trip_status supplies it.
        timezone: The trip's IANA zone.
    """

    name: str
    description: str
    messages: tuple[BaseMessage, ...]
    expected_calls: tuple[ExpectedCall, ...]
    trip_start_date: str | None = TRIP_START
    local_date: str = TODAY
    timezone: str = TRIP_ZONE


# The trip so far, as get_all_expenses returns it. Ids use the bot's id alphabet.
EXPENSE_LIST: Final = "\n".join(
    [
        "number | id | summary | category | amount | date | payment_method",
        "1 | k7m2 | Bibimbap lunch | Food | 12000 KRW | 2026-10-08 | Card",
        "2 | p9x4 | Taxi to Myeongdong | Transport | 8500 KRW | 2026-10-08 | Card",
        "3 | r3dq | Coffee | Food | 5500 KRW | 2026-10-09 | Card",
        "4 | w8tn | Bread | Food | 3200 KRW | 2026-10-09 | Card",
    ]
)

# A turn where the user asked for the list and was shown it, numbered and without ids,
# which is what precedes most real edits.
_LISTED: Final[tuple[BaseMessage, ...]] = (
    HumanMessage("show me everything"),
    AIMessage(
        "",
        tool_calls=[{"id": "list-1", "name": "get_all_expenses", "args": {}}],
    ),
    ToolMessage(EXPENSE_LIST, tool_call_id="list-1", name="get_all_expenses"),
    AIMessage(
        "Here's the trip so far 🐾\n"
        "1. Bibimbap lunch, Food, 12000 KRW, 8 Oct, Card\n"
        "2. Taxi to Myeongdong, Transport, 8500 KRW, 8 Oct, Card\n"
        "3. Coffee, Food, 5500 KRW, 9 Oct, Card\n"
        "4. Bread, Food, 3200 KRW, 9 Oct, Card"
    ),
)


SCENARIOS: Final[tuple[Scenario, ...]] = (
    Scenario(
        name="add_simple",
        description="One expense in the local currency; category inferred, Card default",
        messages=(HumanMessage("lunch 12000 won"),),
        expected_calls=(
            ExpectedCall(
                "add_expense",
                {
                    "amount": "12000",
                    "currency": "KRW",
                    "category": "Food",
                    "date": _TODAY_OR_DEFAULT,
                    "payment_method": _CARD_OR_DEFAULT,
                },
            ),
        ),
    ),
    Scenario(
        name="add_yesterday_cash",
        description="A relative date and a named payment method",
        messages=(HumanMessage("taxi 8500 won yesterday, paid cash"),),
        expected_calls=(
            ExpectedCall(
                "add_expense",
                {
                    "amount": "8500",
                    "currency": "KRW",
                    "category": "Transport",
                    "date": YESTERDAY,
                    "payment_method": "Cash",
                },
            ),
        ),
    ),
    Scenario(
        name="add_weekday",
        description="A weekday name resolved against today's date",
        messages=(HumanMessage("dinner on Tuesday was 45000 won"),),
        expected_calls=(
            ExpectedCall(
                "add_expense",
                {
                    "amount": "45000",
                    "currency": "KRW",
                    "category": "Food",
                    "date": LAST_TUESDAY,
                },
            ),
        ),
    ),
    Scenario(
        name="add_dollar_sign",
        description='"$" means SGD, not USD',
        messages=(HumanMessage("$15 for sunscreen"),),
        expected_calls=(
            ExpectedCall(
                "add_expense",
                {
                    "amount": "15",
                    "currency": "SGD",
                    "category": ("Shopping", "Misc"),
                },
            ),
        ),
    ),
    Scenario(
        name="add_two_in_one",
        description="Two expenses in one message become two calls",
        messages=(HumanMessage("coffee 5500 and bread 3200 won"),),
        expected_calls=(
            ExpectedCall(
                "add_expense", {"amount": "5500", "currency": "KRW", "category": "Food"}
            ),
            ExpectedCall(
                "add_expense", {"amount": "3200", "currency": "KRW", "category": "Food"}
            ),
        ),
    ),
    Scenario(
        name="edit_amount",
        description="An edit names the right id and its current amount",
        messages=(*_LISTED, HumanMessage("the taxi was actually 9000 won")),
        expected_calls=(
            ExpectedCall(
                "edit_expense",
                {"expense_id": "p9x4", "expected_amount": "8500", "amount": "9000"},
            ),
        ),
    ),
    Scenario(
        name="edit_two_payment",
        description="One message editing two expenses, each by its own id",
        messages=(*_LISTED, HumanMessage("the coffee and the bread were paid in cash")),
        expected_calls=(
            ExpectedCall(
                "edit_expense",
                {
                    "expense_id": "r3dq",
                    "expected_amount": "5500",
                    "payment_method": "Cash",
                },
            ),
            ExpectedCall(
                "edit_expense",
                {
                    "expense_id": "w8tn",
                    "expected_amount": "3200",
                    "payment_method": "Cash",
                },
            ),
        ),
    ),
    Scenario(
        name="delete_one",
        description="A delete names the right id and its current amount",
        messages=(*_LISTED, HumanMessage("delete the coffee")),
        expected_calls=(
            ExpectedCall(
                "delete_expense", {"expense_id": "r3dq", "expected_amount": "5500"}
            ),
        ),
    ),
    Scenario(
        name="edit_without_list",
        description="An edit with no ids in the conversation lists the expenses first",
        messages=(HumanMessage("change the taxi to cash"),),
        expected_calls=(ExpectedCall("get_all_expenses"),),
    ),
    Scenario(
        name="show_all",
        description="A request to see the expenses",
        messages=(HumanMessage("what have we spent so far?"),),
        expected_calls=(ExpectedCall("get_all_expenses"),),
    ),
    Scenario(
        name="greeting",
        description="Chat that is not an expense calls no tool",
        messages=(HumanMessage("hi"),),
        expected_calls=(),
    ),
    Scenario(
        name="end_trip",
        description="Ending a trip lists the expenses first, as the prompt instructs",
        messages=(HumanMessage("end trip"),),
        expected_calls=(ExpectedCall("get_all_expenses"),),
    ),
    Scenario(
        name="moved_zone",
        description="Arriving in another time zone moves the trip",
        messages=(HumanMessage("we just landed in Tokyo!"),),
        expected_calls=(ExpectedCall("set_trip_timezone", {"timezone": "Asia/Tokyo"}),),
    ),
    Scenario(
        name="start_trip",
        description="Starting a trip with a destination sets its zone",
        messages=(HumanMessage("starting a trip to Osaka today"),),
        expected_calls=(ExpectedCall("start_trip", {"timezone": "Asia/Tokyo"}),),
        trip_start_date=None,
        timezone="Asia/Singapore",
    ),
    Scenario(
        name="start_trip_no_place",
        description="Starting a trip with no destination asks where, calling no tool",
        messages=(HumanMessage("start a trip"),),
        expected_calls=(),
        trip_start_date=None,
        timezone="Asia/Singapore",
    ),
)
