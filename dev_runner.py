"""Interactive dev runner for testing the expenses bot agent without Telegram.

Invokes the LangGraph agent graph directly from the terminal. Useful during
development — LangSmith traces are emitted automatically if LANGSMITH_API_KEY
is in .env.

Usage:
    uv run python dev_runner.py
    uv run python dev_runner.py --user-id 99999
"""

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, ToolMessage

from src.bot.agent.graph import END_TRIP_NODE, build_graph, clear_thread_history

SGT = ZoneInfo("Asia/Singapore")


def run(user_id: str) -> None:
    """Run the interactive REPL for the given simulated user ID.

    Maintains conversation state across turns via the DynamoDB checkpointer,
    keyed by thread_id. Handles the end_trip interrupt by prompting for
    confirmation before allowing the graph to proceed.

    Args:
        user_id: Simulated Telegram user ID string used as the DynamoDB thread key.
    """
    graph = build_graph()
    thread_id = f"dev#{user_id}"
    config = {"configurable": {"thread_id": thread_id}}

    print(f"Dev runner — user_id={user_id}. Type 'exit' to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        state = graph.get_state(config)
        trip_ended = False

        if END_TRIP_NODE in (state.next or ()):
            # Graph is paused before end_trip_node — treat this turn as the confirmation.
            if user_input.lower() in ("y", "yes"):
                result = graph.invoke(None, config)
                trip_ended = True
            else:
                last_ai = state.values["messages"][-1]
                tool_call_id = last_ai.tool_calls[0]["id"]
                graph.update_state(
                    config,
                    {
                        "messages": [
                            ToolMessage(
                                content="User cancelled ending the trip.",
                                tool_call_id=tool_call_id,
                            )
                        ]
                    },
                    as_node="end_trip_node",
                )
                result = graph.invoke(None, config)
        else:
            message_date = datetime.now(SGT).strftime("%Y-%m-%d")
            result = graph.invoke(
                {
                    "messages": [HumanMessage(content=user_input)],
                    "telegram_user_id": user_id,
                    "message_date": message_date,
                },
                config,
            )

        state_after = graph.get_state(config)
        if state_after.next:
            print(
                "Bot: [About to end your trip — type 'yes' to confirm or anything else to cancel]\n"
            )
        else:
            last_msg = result["messages"][-1]
            print(f"Bot: {last_msg.content}\n")

        if trip_ended:
            # After the summary has been printed — it was written from this history.
            clear_thread_history(graph, thread_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dev runner for the expenses bot agent."
    )
    parser.add_argument(
        "--user-id",
        default="dev_user_1",
        help="Simulated Telegram user ID (default: dev_user_1)",
    )
    args = parser.parse_args()
    run(args.user_id)


if __name__ == "__main__":
    main()
