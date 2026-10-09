"""LangGraph node functions for the expenses bot agent graph."""

from collections.abc import Sequence
from typing import Any

import boto3
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately

from src.bot.agent.prompts import get_system_prompt
from src.bot.agent.state import AgentState
from src.bot.config import settings
from src.bot.storage.dynamodb import get_item
from src.bot.timezones import DEFAULT_TIMEZONE, local_date
from src.bot.tools import expenses, trip

bedrock_client = boto3.client("bedrock-runtime", region_name=settings.AWS_REGION)
llm = ChatBedrockConverse(
    client=bedrock_client, model_id=settings.AWS_BEDROCK_MODEL_ID, temperature=0.3
)
tools = [
    expenses.add_expense,
    expenses.edit_expense,
    expenses.delete_expense,
    expenses.get_all_expenses,
    trip.start_trip,
    trip.set_trip_timezone,
    trip.end_trip,
]

llm_with_tools = llm.bind_tools(tools)


def check_trip_status(state: AgentState) -> dict[str, Any]:
    """Read the ledger's active trip and work out today's date in the trip's time zone.

    Runs at the start of every turn and again after every tool round, so a start_trip or
    set_trip_timezone earlier in the same turn is reflected before the model writes its
    next step. This is the one place the message's UTC time becomes a local date.

    Args:
        state: Current agent state containing ledger_id and message_time.

    Returns:
        Partial state update with trip_start_date (None when no trip is active),
        trip_timezone (the trip's IANA zone, or the default zone when there is no trip or
        the trip predates time zones), and local_date ('YYYY-MM-DD' in that zone).

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
        ValueError: If message_time is not a timezone-aware ISO-8601 timestamp.
    """
    item = get_item(f"USER#{state['ledger_id']}", "TRIP#ACTIVE")
    timezone = (item or {}).get("timezone") or DEFAULT_TIMEZONE
    return {
        "trip_start_date": item["start_date"] if item else None,
        "trip_timezone": timezone,
        "local_date": local_date(state["message_time"], timezone),
    }


def bounded_history(
    messages: Sequence[BaseMessage], token_budget: int
) -> list[BaseMessage]:
    """The most recent part of the conversation, for sending to the model.

    Cuts only at the start of a user message, so a tool call is never separated from its
    result and the window never opens mid-exchange. The current turn — the latest user
    message and everything after it — is always kept whole, even past the budget,
    because the model needs its own tool results to finish the turn.

    Args:
        messages: The full conversation from the checkpoint.
        token_budget: Approximate token ceiling for the returned history.

    Returns:
        A suffix of messages that starts with a user message, or every message when there
        is no user message at all.
    """
    last_user = max(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=None
    )
    if last_user is None:
        return list(messages)

    window = trim_messages(
        messages,
        strategy="last",
        token_counter=count_tokens_approximately,
        max_tokens=token_budget,
        start_on="human",
        allow_partial=False,
    )
    # Empty when the current turn alone exceeds the budget; it is sent whole regardless.
    return window or list(messages[last_user:])


def agent_node(state: AgentState) -> dict[str, Any]:
    """Invoke the LLM with the current message history and system prompt.

    Builds a system prompt reflecting whether the user has an active trip, then
    calls the Bedrock-hosted LLM with the recent part of the conversation — see
    bounded_history. The LLM responds with either a plain text reply or tool call
    requests.

    Args:
        state: Current agent state containing messages, plus the trip_start_date,
            trip_timezone and local_date written by check_trip_status.

    Returns:
        Partial state update with the LLM's response appended to messages.

    Raises:
        botocore.exceptions.ClientError: If the Bedrock request fails.
    """
    sys_prompt = SystemMessage(
        get_system_prompt(
            state["trip_start_date"], state["local_date"], state["trip_timezone"]
        )
    )
    history = bounded_history(
        list(state["messages"]), settings.MODEL_HISTORY_TOKEN_BUDGET
    )
    response = llm_with_tools.invoke([sys_prompt, *history])

    return {"messages": [response]}
