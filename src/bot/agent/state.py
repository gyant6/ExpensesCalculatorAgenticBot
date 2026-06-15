"""LangGraph state definition for the expenses bot agent."""

from typing import NotRequired

from langgraph.graph import MessagesState


class AgentState(MessagesState):
    telegram_user_id: str
    message_date: str
    pie_chart_bytes: NotRequired[bytes | None]
    bar_chart_bytes: NotRequired[bytes | None]
