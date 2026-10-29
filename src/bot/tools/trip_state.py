"""The active-trip marker's key and lifecycle, shared by the trip and expense tools.

Kept apart from trip.py because the expense tools need the guard below, and trip.py
already imports from expenses.py.
"""

from enum import StrEnum
from typing import Final

from src.bot.storage import dynamodb

TRIP_ACTIVE_SK: Final = "TRIP#ACTIVE"

# Attribute on the TRIP#ACTIVE item holding a TripStatus. Absent on trips started before
# it existed, which count as ACTIVE.
STATUS_ATTRIBUTE: Final = "status"

TRIP_ENDING: Final = (
    "The trip is being ended right now, so nothing was recorded or changed. Tell the "
    "user it was not saved. If the trip does not finish ending, they can ask to end "
    "it again."
)


class TripStatus(StrEnum):
    """Where the active trip is in its lifecycle, stored in its `status` attribute.

    Members:
        ACTIVE: Expenses can be added, edited and deleted.
        ENDING: end_trip is exporting, archiving and deleting the trip's expenses. Every
            expense write is refused, so none can land after end_trip has read them and
            be left behind for the next trip. end_trip sets ACTIVE again if it fails
            before archiving; a trip left ENDING by a crash finishes when end_trip runs
            again.
    """

    ACTIVE = "ACTIVE"
    ENDING = "ENDING"


def trip_not_ending(ledger_id: str) -> dynamodb.Guard:
    """The guard every expense write carries: the ledger's trip must not be ending.

    A ledger with no trip passes, so expenses recorded before a trip starts behave as
    they always have.

    Args:
        ledger_id: The ledger (Telegram chat ID) whose trip is checked.

    Returns:
        A guard for dynamodb's guarded_* writes.
    """
    return dynamodb.Guard(
        pk=f"USER#{ledger_id}",
        sk=TRIP_ACTIVE_SK,
        attribute=STATUS_ATTRIBUTE,
        forbidden_value=TripStatus.ENDING.value,
    )
