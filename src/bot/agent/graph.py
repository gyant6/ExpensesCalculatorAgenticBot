"""Compiled LangGraph agent graph for the expenses bot."""

import logging

from botocore.exceptions import ClientError
from langchain_core.messages import AIMessage, ToolMessage
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
        "end_trip" if end_trip is the sole tool call (routed to end_trip_node, which is
        interrupted before execution for user confirmation), "end_trip_batch_error" if
        end_trip was batched with other tools (the error node rejects the batch so the
        LLM retries with end_trip alone), "tools" if only non-end_trip tools were
        requested, or END if the LLM returned a plain text response. Only an AIMessage
        can carry tool calls, so anything else also ends the turn.
    """
    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
        return END
    names = {tc["name"] for tc in last_message.tool_calls}
    if trip.end_trip.name in names:
        if len(last_message.tool_calls) > 1:
            return "end_trip_batch_error"
        return "end_trip"
    return "tools"


def end_trip_batch_error_node(state: AgentState) -> dict[str, list[ToolMessage]]:
    """Reject a tool batch that mixes end_trip with other tools.

    The LLM occasionally batches end_trip alongside other tools (e.g. get_all_expenses).
    Routing that batch to tools_node would silently skip end_trip (it is not bound there),
    and routing to end_trip_node would silently skip the other tools. This node injects
    an error ToolMessage for every call in the batch so the LLM sees honest feedback and
    retries with end_trip as its only call.

    Args:
        state: Current agent state; the last message must be an AIMessage with tool calls.

    Returns:
        A state delta containing one ToolMessage per tool call in the batch.
    """
    last_message = state["messages"][-1]
    return {
        "messages": [
            ToolMessage(
                content=(
                    "end_trip must be called alone, without any other tools in the same"
                    " turn. Please retry using only end_trip."
                ),
                tool_call_id=tc["id"],
                name=tc["name"],
            )
            for tc in last_message.tool_calls  # type: ignore[union-attr]
        ]
    }


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
    workflow.add_node("end_trip_batch_error_node", end_trip_batch_error_node)

    workflow.add_edge(START, "check_trip_status")
    workflow.add_edge("check_trip_status", "agent_node")

    workflow.add_conditional_edges(
        "agent_node",
        custom_routes,
        {
            "tools": "tools_node",
            "end_trip": END_TRIP_NODE,
            "end_trip_batch_error": "end_trip_batch_error_node",
            END: END,
        },
    )
    workflow.add_edge("tools_node", "agent_node")
    workflow.add_edge(END_TRIP_NODE, "agent_node")
    workflow.add_edge("end_trip_batch_error_node", "agent_node")

    checkpointer = DynamoDBSaver(
        table_name=settings.DYNAMODB_TABLE_NAME,
        endpoint_url=settings.DYNAMODB_ENDPOINT_URL,
        region_name=settings.AWS_REGION,
        # Each checkpoint is a full snapshot of the message history and one is written per
        # graph step, so the same conversation is stored many times over. Compression cuts
        # the DynamoDB write and read units that costs; it does not affect the tokens sent
        # to Bedrock, which sees the state decompressed.
        enable_checkpoint_compression=True,
        # Writes a `ttl` epoch attribute on every checkpoint. DynamoDB's TTL process
        # deletes items past that timestamp, cleaning up abandoned threads automatically.
        ttl_seconds=settings.CHECKPOINT_TTL_SECONDS,
    )

    graph = workflow.compile(
        checkpointer=checkpointer, interrupt_before=[END_TRIP_NODE]
    )

    return graph
