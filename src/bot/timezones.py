"""Turning a message's moment in time into the calendar day the traveller is living.

A Telegram message carries a UTC timestamp. The day an expense belongs to is that moment
in the trip's own time zone: 02:51 UTC on 15 Sep is still 14 Sep in Los Angeles. This is
the only place a time-zone conversion happens — the model is told the resulting date and
never converts zones itself.
"""

from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Used when no trip is active, and for a trip recorded before trips carried a zone.
DEFAULT_TIMEZONE: Final = "Asia/Singapore"


def is_valid_timezone(name: str) -> bool:
    """Return True if the name is an IANA time zone the system recognises.

    Args:
        name: A candidate IANA name, e.g. 'Asia/Tokyo'.

    Returns:
        True if `zoneinfo` can load it, False for an unknown or malformed name.
    """
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def local_date(message_time: str, timezone: str) -> str:
    """The calendar day, in a given zone, at which a message was sent.

    Args:
        message_time: The message's timestamp as timezone-aware ISO-8601, e.g.
            '2026-09-15T02:51:00+00:00'.
        timezone: An IANA zone name; must satisfy `is_valid_timezone`.

    Returns:
        The local date as 'YYYY-MM-DD'.

    Raises:
        ValueError: If message_time is not ISO-8601 or carries no UTC offset — a naive
            time cannot be placed in any zone.
        zoneinfo.ZoneInfoNotFoundError: If the zone name is unknown.
    """
    moment = datetime.fromisoformat(message_time)
    if moment.tzinfo is None:
        raise ValueError(f"message_time has no UTC offset: {message_time!r}")
    return moment.astimezone(ZoneInfo(timezone)).date().isoformat()


def describe_date(iso_date: str) -> str:
    """Spell a date out with its weekday, for the model to reason about.

    Args:
        iso_date: A date as 'YYYY-MM-DD'.

    Returns:
        The date as e.g. 'Monday, 14 September 2026'. The weekday is what lets the
        model resolve "on Tuesday" without counting days itself.

    Raises:
        ValueError: If iso_date is not a valid 'YYYY-MM-DD' date.
    """
    day = date.fromisoformat(iso_date)
    return f"{day:%A}, {day.day} {day:%B %Y}"
