"""Conversation operations shared by Telegram, the terminal and model evaluation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot

from src.bot.agent.graph import CHECKPOINT_DURABILITY, END_TRIP_NODE, thread_id_for
from src.bot.storage import dynamodb


class PendingConfirmationError(ValueError):
    """A new message arrived while trip-end confirmation is pending."""


class NoPendingConfirmationError(ValueError):
    """A confirmation or cancellation has no pending trip end to act on."""


@dataclass(frozen=True)
class ConfirmationResult:
    """A resumed graph result and whether the active-trip marker was removed."""

    result: dict[str, Any]
    trip_ended: bool


def config_for(ledger_id: str) -> RunnableConfig:
    """Return the versioned checkpoint configuration for one ledger."""
    return {"configurable": {"thread_id": thread_id_for(ledger_id)}}


def awaiting_confirmation(state: StateSnapshot) -> bool:
    """Return whether a snapshot is paused before the trip-end tool."""
    return END_TRIP_NODE in (state.next or ())


def send_message(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    ledger_id: str,
    text: str,
    message_time: str,
    *,
    state: StateSnapshot | None = None,
) -> dict[str, Any]:
    """Send text with the caller's original UTC timestamp to the real graph.

    A caller may supply its freshly read state to avoid a duplicate checkpoint read.
    Raises PendingConfirmationError if the trip must be confirmed first; graph and
    storage failures propagate. This function never clears conversation history.
    """
    config = config_for(ledger_id)
    if awaiting_confirmation(state if state is not None else graph.get_state(config)):
        raise PendingConfirmationError(
            "Please confirm or cancel the pending trip ending first."
        )
    result: dict[str, Any] = graph.invoke(
        {
            "messages": [HumanMessage(content=text)],
            "ledger_id": ledger_id,
            "message_time": message_time,
        },
        config,
        durability=CHECKPOINT_DURABILITY,
    )
    return result


def confirm_end_trip(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    ledger_id: str,
    *,
    state: StateSnapshot | None = None,
    trip_reader: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> ConfirmationResult:
    """Resume a pending trip end and verify its active-trip marker is gone.

    Raises NoPendingConfirmationError for a stale confirmation. Graph and storage
    failures propagate. The caller prepares attachments before this call, delivers
    the result, then clears history; a failed resume must not be reported as success.
    """
    config = config_for(ledger_id)
    if not awaiting_confirmation(
        state if state is not None else graph.get_state(config)
    ):
        raise NoPendingConfirmationError(
            "This confirmation has already been processed."
        )
    result = graph.invoke(None, config, durability=CHECKPOINT_DURABILITY)
    read = (
        trip_reader
        if trip_reader is not None
        else partial(dynamodb.get_item, consistent_read=True)
    )
    return ConfirmationResult(
        result=result, trip_ended=read(f"USER#{ledger_id}", "TRIP#ACTIVE") is None
    )


def cancel_end_trip(
    graph: CompiledStateGraph,  # type: ignore[type-arg]
    ledger_id: str,
    *,
    state: StateSnapshot | None = None,
) -> dict[str, Any]:
    """Resolve the pending tool call as cancelled and let the agent reply.

    Raises NoPendingConfirmationError if no trip end is pending and ValueError for
    malformed pending state. Graph/storage failures propagate. Trip records and
    conversation history are retained.
    """
    config = config_for(ledger_id)
    current = state if state is not None else graph.get_state(config)
    if not awaiting_confirmation(current):
        raise NoPendingConfirmationError(
            "This confirmation has already been processed."
        )
    pending = current.values["messages"][-1].tool_calls
    if len(pending) != 1 or pending[0]["name"] != "end_trip":
        raise ValueError("Pending confirmation must contain exactly one end_trip call")
    graph.update_state(
        config,
        {
            "messages": [
                ToolMessage(
                    content="User cancelled ending the trip.",
                    tool_call_id=pending[0]["id"],
                    name=pending[0]["name"],
                )
            ]
        },
        END_TRIP_NODE,
    )
    result: dict[str, Any] = graph.invoke(
        None, config, durability=CHECKPOINT_DURABILITY
    )
    return result


def extract_text(content: str | list[Any]) -> str:
    """Return user-facing text, excluding tool and reasoning content blocks."""
    if isinstance(content, str):
        return content
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
