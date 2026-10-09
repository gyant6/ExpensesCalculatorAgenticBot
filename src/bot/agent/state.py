"""LangGraph state definition for the expenses bot agent."""

from typing import NotRequired

from langgraph.graph import MessagesState


class AgentState(MessagesState):
    ledger_id: str
    # The incoming message's own timestamp, timezone-aware ISO-8601 in UTC. Its own time
    # rather than the processing time, so a delayed or redelivered update keeps its day.
    message_time: str
    # Written by check_trip_status at the start of every turn and after every tool round.
    trip_start_date: NotRequired[str | None]
    # The active trip's IANA zone, or the default zone when no trip is active.
    trip_timezone: NotRequired[str]
    # message_time as a calendar day in trip_timezone, 'YYYY-MM-DD'.
    local_date: NotRequired[str]
