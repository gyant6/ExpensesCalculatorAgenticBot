"""Compiled LangGraph agent graph for the expenses bot."""

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from src.bot.agent.nodes import agent_node, check_trip_status, tools
from src.bot.agent.state import AgentState


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
    workflow.add_node("tools_node", ToolNode(tools))

    workflow.add_edge(START, "check_trip_status")
    workflow.add_edge("check_trip_status", "agent_node")

    workflow.add_conditional_edges(
        "agent_node", tools_condition, {"tools": "tools_node", END: END}
    )
    workflow.add_edge("tools_node", "agent_node")

    app = workflow.compile()

    return app
