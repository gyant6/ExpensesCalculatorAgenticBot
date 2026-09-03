"""LangGraph state definition for the expenses bot agent."""

from typing import NotRequired

from langgraph.graph import MessagesState


class AgentState(MessagesState):
    ledger_id: str
    message_date: str
    trip_start_date: NotRequired[str | None]
