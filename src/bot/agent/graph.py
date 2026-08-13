"""Compiled LangGraph agent graph for the expenses bot."""

import logging

from botocore.exceptions import ClientError
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph_checkpoint_aws import DynamoDBSaver

from src.bot.agent.nodes import agent_node, check_trip_status
from src.bot.agent.state import AgentState
from src.bot.config import settings
from src.bot.tools import expenses, trip

logger = logging.getLogger(__name__)

# Node name shared with telegram_handler, which inspects graph.get_state(...).next to
# detect that the graph is paused awaiting end_trip confirmation.
END_TRIP_NODE = "end_trip_node"


def clear_thread_history(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    thread_id: str,
) -> None:
    """Delete every checkpoint and pending write for one conversation thread.

    Call this once a trip's summary has been delivered. `agent_node` replays the entire
    `messages` list to Bedrock on every turn, so an ended trip's history would otherwise
    inflate the cost and latency of every later message indefinitely, and grow the
    checkpoint item towards DynamoDB's 400 KB per-item limit.

    Must not be called before the summary is produced: the summary is written by
    `agent_node` after `end_trip` returns, and it reads the history this deletes.

    Failures are logged rather than raised. By the time this runs the user already has
    their summary and the trip records are gone, so surfacing an error would report a
    failure for work that has already succeeded.

    Args:
        graph: The compiled graph whose checkpointer holds the thread.
        thread_id: The thread ID to clear, matching the one used in the graph config.
    """
    checkpointer = graph.checkpointer
    if not isinstance(checkpointer, BaseCheckpointSaver):
        logger.error(
            "Graph has no checkpointer; conversation history for thread %s was not cleared.",
            thread_id,
        )
        return

    try:
        checkpointer.delete_thread(thread_id)
    except ClientError:
        logger.exception(
            "Failed to clear conversation history for thread %s", thread_id
        )


def custom_routes(state: AgentState) -> str:
    """Route after agent_node based on the last message's tool calls.

    Args:
        state: Current agent state; only the final entry of messages is inspected.

    Returns:
        "end_trip" if the LLM requested the end_trip tool (routed to end_trip_node,
        which is interrupted before execution for user confirmation), "tools" if any
        other tool was requested, or END if the LLM returned a plain text response.
        Only an AIMessage can carry tool calls, so anything else also ends the turn.
    """
    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return END
    if last_message.tool_calls[0]["name"] == trip.end_trip.name:
        return "end_trip"
    return "tools"


def build_graph() -> CompiledStateGraph:  # type: ignore[type-arg]
    """Build and compile the LangGraph agent graph.

    Graph flow:
        START → check_trip_status → agent_node → END
                                         ↑  ↓ (if tool calls)
                                         └─ tools_node

    Returns:
        The compiled LangGraph application ready to invoke with AgentState.
    """
    workflow = StateGraph(AgentState)

    workflow.add_node("check_trip_status", check_trip_status)
    workflow.add_node("agent_node", agent_node)
    workflow.add_node(
        "tools_node",
        ToolNode(
            [
                trip.start_trip,
                expenses.add_expense,
                expenses.edit_expense,
                expenses.delete_expense,
                expenses.get_all_expenses,
            ]
        ),
    )
    workflow.add_node(END_TRIP_NODE, ToolNode([trip.end_trip]))

    workflow.add_edge(START, "check_trip_status")
    workflow.add_edge("check_trip_status", "agent_node")

    workflow.add_conditional_edges(
        "agent_node",
        custom_routes,
        {"tools": "tools_node", "end_trip": END_TRIP_NODE, END: END},
    )
    workflow.add_edge("tools_node", "agent_node")
    workflow.add_edge(END_TRIP_NODE, "agent_node")

    checkpointer = DynamoDBSaver(
        table_name=settings.DYNAMODB_TABLE_NAME,
        endpoint_url=settings.DYNAMODB_ENDPOINT_URL,
        region_name=settings.AWS_REGION,
        # Each checkpoint is a full snapshot of the message history and one is written per
        # graph step, so the same conversation is stored many times over. Compression cuts
        # the DynamoDB write and read units that costs; it does not affect the tokens sent
        # to Bedrock, which sees the state decompressed.
        enable_checkpoint_compression=True,
    )

    graph = workflow.compile(
        checkpointer=checkpointer, interrupt_before=[END_TRIP_NODE]
    )

    return graph
