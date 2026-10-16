"""Interactive dev runner for testing the expenses bot agent without Telegram.

Invokes the LangGraph agent graph directly from the terminal. Useful during
development — LangSmith traces are emitted automatically if LANGSMITH_API_KEY
is in .env.

Usage:
    uv run python dev_runner.py
    uv run python dev_runner.py --user-id 99999
"""

import argparse
from datetime import datetime, timezone

from src.bot.agent.graph import (
    build_graph,
    clear_thread_history,
    thread_id_for,
)
from src.bot.conversation import (
    awaiting_confirmation,
    cancel_end_trip,
    config_for,
    confirm_end_trip,
    extract_text,
    send_message,
)


def run(user_id: str) -> None:
    """Run the interactive REPL for the given simulated user ID.

    Maintains conversation state across turns via the DynamoDB checkpointer,
    using the same thread ID format as the Telegram handlers so copied checkpoints
    can be resumed locally. Handles the end_trip interrupt by prompting for
    confirmation before allowing the graph to proceed.

    Args:
        user_id: Simulated Telegram user ID string used as the DynamoDB thread key.
    """
    graph = build_graph()
    thread_id = thread_id_for(user_id)
    config = config_for(user_id)

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

        if awaiting_confirmation(state):
            # Graph is paused before end_trip_node — treat this turn as the confirmation.
            if user_input.lower() in ("y", "yes"):
                confirmation = confirm_end_trip(graph, user_id, state=state)
                result = confirmation.result
                if not confirmation.trip_ended:
                    print("Bot: Could not end the trip. Please try again.\n")
                    clear_thread_history(graph, thread_id)
                    continue
                trip_ended = confirmation.trip_ended
            else:
                result = cancel_end_trip(graph, user_id, state=state)
        else:
            # UTC, as Telegram sends it; the graph converts to the trip's local day.
            message_time = datetime.now(timezone.utc).isoformat()
            result = send_message(graph, user_id, user_input, message_time, state=state)

        state_after = graph.get_state(config)
        if awaiting_confirmation(state_after):
            print(
                "Bot: [About to end your trip — type 'yes' to confirm or anything else to cancel]\n"
            )
        else:
            last_msg = result["messages"][-1]
            print(f"Bot: {extract_text(last_msg.content) or 'Done.'}\n")

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
