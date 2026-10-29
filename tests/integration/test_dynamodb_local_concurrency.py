"""Race concurrent tool calls against DynamoDB Local, where conditional writes are real.

Two group members messaging at once are handled by two Lambda invocations running the
same tools at the same time. Each test releases two calls together and checks that
exactly one of them takes effect, the way it would if they had arrived one after the
other. A race window is narrow, so each one is run over several fresh ledgers.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Final
from uuid import uuid4

import pytest

from src.bot.storage import dynamodb
from src.bot.tools import expenses, trip
from src.bot.tools.trip_state import TRIP_ACTIVE_SK, TRIP_ENDING
from tests.integration.conftest import requires_dynamodb_local

pytestmark = requires_dynamodb_local

ROUNDS: Final = 15
_START_TIMEOUT_SECONDS: Final = 10
_CALL_TIMEOUT_SECONDS: Final = 30

MESSAGE_TIME: Final = "2026-10-25T15:30:00+00:00"
NOW: Final = "2026-10-25T15:30:00.000000+00:00"
EDIT_SUCCESS: Final = "Edit expense successful."
DELETE_SUCCESS: Final = "Expense deleted."


def race(*calls: Callable[[], str]) -> list[str]:
    """Run every call on its own thread, releasing them together.

    Args:
        calls: Zero-argument callables returning a tool's result string.

    Returns:
        Each call's result, in the order the calls were given.

    Raises:
        threading.BrokenBarrierError: If a thread failed to start in time.
        Exception: Whatever a call itself raised.
    """
    barrier = threading.Barrier(len(calls))

    def run(call: Callable[[], str]) -> str:
        barrier.wait(timeout=_START_TIMEOUT_SECONDS)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(run, call) for call in calls]
        return [future.result(timeout=_CALL_TIMEOUT_SECONDS) for future in futures]


def new_ledger() -> str:
    """A ledger no other round or test uses."""
    return f"race-{uuid4().hex}"


def seed_expense(ledger_id: str, amount: str = "1200") -> str:
    """Store one expense as add_expense would, and return its id."""
    expense_id = "k7qm"
    dynamodb.put_item(
        {
            "PK": f"USER#{ledger_id}",
            "SK": f"{expenses.EXPENSE_SK_PREFIX}{expense_id}",
            "source_message": "Lunch 1200 yen",
            "summary": "Lunch",
            "category": "Food",
            "amount": Decimal(amount),
            "currency": "JPY",
            "date": "2026-10-25",
            "timezone": "Asia/Tokyo",
            "payment_method": "Card",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    return expense_id


def stored_expense(ledger_id: str, expense_id: str) -> dict[str, Any] | None:
    """Read an expense back with a strongly consistent read."""
    return dynamodb.get_item(
        f"USER#{ledger_id}",
        f"{expenses.EXPENSE_SK_PREFIX}{expense_id}",
        consistent_read=True,
    )


def start(ledger_id: str, timezone: str) -> str:
    """Invoke start_trip as the graph's tool node would."""
    return str(
        trip.start_trip.invoke(
            {"ledger_id": ledger_id, "message_time": MESSAGE_TIME, "timezone": timezone}
        )
    )


def edit(ledger_id: str, expense_id: str, amount: str, message: str) -> str:
    """Invoke edit_expense with the seeded amount as the expected amount."""
    return str(
        expenses.edit_expense.invoke(
            {
                "ledger_id": ledger_id,
                "expense_id": expense_id,
                "expected_amount": "1200",
                "edit_message": message,
                "amount": amount,
            }
        )
    )


def delete(ledger_id: str, expense_id: str) -> str:
    """Invoke delete_expense with the seeded amount as the expected amount."""
    return str(
        expenses.delete_expense.invoke(
            {
                "ledger_id": ledger_id,
                "expense_id": expense_id,
                "expected_amount": "1200",
            }
        )
    )


def test_concurrent_trip_starts_create_exactly_one_trip(local_table: None) -> None:
    """Two simultaneous starts must not both report success and overwrite each other."""
    for _ in range(ROUNDS):
        ledger_id = new_ledger()

        tokyo, london = race(
            lambda: start(ledger_id, "Asia/Tokyo"),
            lambda: start(ledger_id, "Europe/London"),
        )

        started = [r for r in (tokyo, london) if r.startswith("New trip started")]
        assert len(started) == 1, (tokyo, london)
        assert "There is already an active trip." in (tokyo, london)
        active = dynamodb.get_item(
            f"USER#{ledger_id}", "TRIP#ACTIVE", consistent_read=True
        )
        assert active is not None
        winner = "Asia/Tokyo" if tokyo in started else "Europe/London"
        assert active["timezone"] == winner


def test_concurrent_edits_of_one_expense_apply_exactly_one(local_table: None) -> None:
    """Both edits passed the same amount check; only the first write may land."""
    for _ in range(ROUNDS):
        ledger_id = new_ledger()
        expense_id = seed_expense(ledger_id)

        to_1400, to_1500 = race(
            lambda: edit(ledger_id, expense_id, "1400", "Lunch was 1400 yen"),
            lambda: edit(ledger_id, expense_id, "1500", "Lunch was 1500 yen"),
        )

        assert [to_1400, to_1500].count(EDIT_SUCCESS) == 1, (to_1400, to_1500)
        item = stored_expense(ledger_id, expense_id)
        assert item is not None
        winning_amount, winning_message, losing_message = (
            ("1400", "Lunch was 1400 yen", "Lunch was 1500 yen")
            if to_1400 == EDIT_SUCCESS
            else ("1500", "Lunch was 1500 yen", "Lunch was 1400 yen")
        )
        assert item["amount"] == Decimal(winning_amount)
        assert winning_message in item["source_message"]
        assert losing_message not in item["source_message"]


def test_concurrent_edit_and_delete_apply_exactly_one(local_table: None) -> None:
    """An expense must not be both edited and deleted, nor reported as both."""
    for _ in range(ROUNDS):
        ledger_id = new_ledger()
        expense_id = seed_expense(ledger_id)

        edited, deleted = race(
            lambda: edit(ledger_id, expense_id, "1400", "Lunch was 1400 yen"),
            lambda: delete(ledger_id, expense_id),
        )

        assert (edited == EDIT_SUCCESS) != (deleted == DELETE_SUCCESS), (
            edited,
            deleted,
        )
        item = stored_expense(ledger_id, expense_id)
        if edited == EDIT_SUCCESS:
            assert item is not None
            assert item["amount"] == Decimal("1400")
        else:
            assert item is None


def add_taxi(ledger_id: str) -> str:
    """Invoke add_expense as a second member's message would."""
    return str(
        expenses.add_expense.invoke(
            {
                "ledger_id": ledger_id,
                "local_date": "2026-10-26",
                "trip_timezone": "Asia/Tokyo",
                "source_message": "Taxi 900 yen",
                "summary": "Taxi",
                "category": "Transport",
                "amount": "900",
                "currency": "JPY",
            }
        )
    )


def end(ledger_id: str) -> str:
    """Invoke end_trip as the confirmed end-trip node would."""
    return str(trip.end_trip.invoke({"ledger_id": ledger_id}))


def archived_ids(ledger_id: str) -> list[str]:
    """Sort keys of every expense in the ledger's trip archives."""
    archives = dynamodb.query_by_prefix(
        f"USER#{ledger_id}", trip.ARCHIVE_SK_PREFIX, consistent_read=True
    )
    return [expense["SK"] for archive in archives for expense in archive["expenses"]]


def test_writes_landing_while_a_trip_ends_are_refused_not_left_behind(
    local_table: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An add or edit arriving mid-way through end_trip must not escape the archive.

    The FX fetch sits between end_trip reading the expenses and deleting them, so
    writing from there reproduces that interleaving deterministically. Before the trip
    was marked ENDING, the add here was recorded and then joined the next trip.
    """
    ledger_id = new_ledger()
    assert start(ledger_id, "Asia/Tokyo").startswith("New trip started")
    expense_id = seed_expense(ledger_id)
    during_end: list[str] = []

    def rates_with_concurrent_writes() -> dict[str, float]:
        during_end.append(add_taxi(ledger_id))
        during_end.append(edit(ledger_id, expense_id, "1400", "Lunch was 1400 yen"))
        return {"JPY": 200.0}

    monkeypatch.setattr(trip, "get_sgd_exchange_rates", rates_with_concurrent_writes)

    result = end(ledger_id)

    assert result.startswith(trip.END_TRIP_SUCCESS)
    assert during_end == [TRIP_ENDING, TRIP_ENDING]
    assert expenses.list_expenses(ledger_id, consistent_read=True) == []
    archives = dynamodb.query_by_prefix(
        f"USER#{ledger_id}", trip.ARCHIVE_SK_PREFIX, consistent_read=True
    )
    assert [e["amount"] for a in archives for e in a["expenses"]] == [Decimal(1200)]


def test_an_add_racing_end_trip_is_never_lost(
    local_table: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whichever wins, a recorded expense is archived or survives; never neither.

    The add either lands before the trip is marked ENDING, and is archived; is refused;
    or lands after the trip is gone, as an expense recorded with no trip does.
    """
    monkeypatch.setattr(trip, "get_sgd_exchange_rates", lambda: {"JPY": 200.0})
    for _ in range(ROUNDS):
        ledger_id = new_ledger()
        assert start(ledger_id, "Asia/Tokyo").startswith("New trip started")
        seed_expense(ledger_id)

        added, ended = race(lambda: add_taxi(ledger_id), lambda: end(ledger_id))

        assert ended.startswith(trip.END_TRIP_SUCCESS), ended
        assert (
            dynamodb.get_item(f"USER#{ledger_id}", TRIP_ACTIVE_SK, consistent_read=True)
            is None
        )
        archived = archived_ids(ledger_id)
        remaining = [
            e["SK"] for e in expenses.list_expenses(ledger_id, consistent_read=True)
        ]
        assert not set(archived) & set(remaining)
        if added == TRIP_ENDING:
            assert len(archived) == 1 and not remaining
        else:
            assert added.startswith("Expense recorded"), added
            assert len(archived) + len(remaining) == 2
            # A recorded taxi is in the CSV exactly when it was archived.
            assert ("Taxi" in ended) == (len(archived) == 2)
