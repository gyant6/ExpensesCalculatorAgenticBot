"""LangGraph node functions for the expenses bot agent graph."""

from typing import Any

import boto3
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import SystemMessage

from src.bot.agent.prompts import get_system_prompt
from src.bot.agent.state import AgentState
from src.bot.config import settings
from src.bot.storage.dynamodb import get_item
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
    trip.end_trip,
]

llm_with_tools = llm.bind_tools(tools)


def check_trip_status(state: AgentState) -> dict[str, Any]:
    """Read the user's active trip from DynamoDB and write trip_start_date into agent state.

    Args:
        state: Current agent state containing telegram_user_id.

    Returns:
        Partial state update with trip_start_date set to the trip's start date string
        (e.g. '2026-06-17') if an active trip exists, or None if no trip is active.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails.
    """
    item = get_item(f"USER#{state['telegram_user_id']}", "TRIP#ACTIVE")
    if not item:
        return {"trip_start_date": None}
    return {"trip_start_date": item["start_date"]}


def agent_node(state: AgentState) -> dict[str, Any]:
    """Invoke the LLM with the current message history and system prompt.

    Builds a system prompt reflecting whether the user has an active trip, then
    calls the Bedrock-hosted LLM with all messages in state. The LLM responds
    with either a plain text reply or tool call requests.

    Args:
        state: Current agent state containing messages and trip_start_date.

    Returns:
        Partial state update with the LLM's response appended to messages.

    Raises:
        botocore.exceptions.ClientError: If the Bedrock request fails.
    """
    sys_prompt = SystemMessage(get_system_prompt(state["trip_start_date"]))
    response = llm_with_tools.invoke([sys_prompt, *list(state["messages"])])

    return {"messages": [response]}
