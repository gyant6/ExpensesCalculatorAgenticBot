"""LangGraph state definition for the expenses bot agent."""

from typing import NotRequired

from langgraph.graph import MessagesState


class AgentState(MessagesState):
    telegram_user_id: str
    message_date: str
    trip_start_date: NotRequired[str | None]
