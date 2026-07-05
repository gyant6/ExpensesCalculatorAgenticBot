"""Compiled LangGraph agent graph for the expenses bot."""

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph_checkpoint_aws import DynamoDBSaver

from src.bot.agent.nodes import agent_node, check_trip_status
from src.bot.agent.state import AgentState
from src.bot.config import settings
from src.bot.tools import expenses, trip


def custom_routes(state: AgentState) -> str:
    """Route after agent_node based on the last message's tool calls.

    Returns:
        "end_trip" if the LLM requested the end_trip tool (routed to end_trip_node,
        which is interrupted before execution for user confirmation), "tools" if any
        other tool was requested, or END if the LLM returned a plain text response.
    """
    if not state["messages"][-1].tool_calls:
        return END
    if state["messages"][-1].tool_calls[0]["name"] == "end_trip":
        return "end_trip"
    else:
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
    workflow.add_node("tools_node", ToolNode([
        trip.start_trip, expenses.add_expense, expenses.edit_expense,
        expenses.delete_expense, expenses.get_all_expenses
    ]))
    workflow.add_node("end_trip_node", ToolNode([trip.end_trip]))

    workflow.add_edge(START, "check_trip_status")
    workflow.add_edge("check_trip_status", "agent_node")

    workflow.add_conditional_edges(
        "agent_node", custom_routes, {"tools": "tools_node", "end_trip": "end_trip_node"}
    )
    workflow.add_edge("tools_node", "agent_node")
    workflow.add_edge("end_trip_node", "agent_node")

    checkpointer = DynamoDBSaver(
        table_name=settings.DYNAMODB_TABLE_NAME,
        endpoint_url=settings.DYNAMODB_ENDPOINT_URL,
        region_name=settings.AWS_REGION,
    )

    graph = workflow.compile(checkpointer=checkpointer, interrupt_before=["end_trip_node"])

    return graph
