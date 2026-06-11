from langgraph.graph import MessagesState


class AgentState(MessagesState):
    telegram_user_id: str
    message_date: str
    pie_chart_bytes: bytes | None = None
    bar_chart_bytes: bytes | None = None
