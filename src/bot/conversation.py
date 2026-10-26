"""Conversation operations shared by Telegram, the terminal and model evaluation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot

from src.bot.agent.graph import (
    CHECKPOINT_DURABILITY,
    END_TRIP_CANCELLED,
    END_TRIP_NODE,
    thread_id_for,
)
from src.bot.export import extract_end_trip_csv, summarize_csv
from src.bot.storage import dynamodb
from src.bot.tools.trip import END_TRIP_SUCCESS

logger = logging.getLogger(__name__)


class PendingConfirmationError(ValueError):
    """A new message arrived while trip-end confirmation is pending."""


class NoPendingConfirmationError(ValueError):
    """A confirmation or cancellation has no pending trip end to act on."""


@dataclass(frozen=True)
class ConfirmationResult:
    """A resumed graph result, verified trip state and the end_trip CSV export."""

    result: dict[str, Any]
    trip_ended: bool
    csv_bytes: bytes | None


def _end_trip_csv(messages: list[Any]) -> bytes | None:
    """Extract the CSV produced by the successful end_trip tool invocation."""
    for message in reversed(messages):
        if not isinstance(message, ToolMessage) or message.name != "end_trip":
            continue
        csv_bytes = extract_end_trip_csv(str(message.content), END_TRIP_SUCCESS)
        if csv_bytes is not None:
            return csv_bytes
    return None


def _end_trip_succeeded(messages: list[Any]) -> bool:
    """Check that this resume contains an end_trip success tool result."""
    return any(
        isinstance(message, ToolMessage)
        and message.name == "end_trip"
        and str(message.content).startswith(END_TRIP_SUCCESS)
        for message in messages
    )


def render_confirmed_trip(
    confirmation: ConfirmationResult, fallback_csv: bytes | None = None
) -> tuple[str, bytes | None]:
    """Return a code-calculated message and its matching CSV for a confirmed trip.

    The graph's model reply is not used for monetary figures. The pre-rendered CSV is
    a fallback if the tool result cannot be recovered after the trip was deleted.
    """
    for csv_bytes in (confirmation.csv_bytes, fallback_csv):
        if csv_bytes is None:
            continue
        try:
            return summarize_csv(csv_bytes), csv_bytes
        except (UnicodeError, ValueError):
            logger.exception("Could not summarize the confirmed trip CSV")
    return "Trip ended, but I couldn't calculate the totals. 🐾", None


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
    current = state if state is not None else graph.get_state(config)
    if not awaiting_confirmation(current):
        raise NoPendingConfirmationError(
            "This confirmation has already been processed."
        )
    previous_count = len(current.values.get("messages", []))
    result = graph.invoke(None, config, durability=CHECKPOINT_DURABILITY)
    read = (
        trip_reader
        if trip_reader is not None
        else partial(dynamodb.get_item, consistent_read=True)
    )
    messages = result["messages"][previous_count:]
    return ConfirmationResult(
        result=result,
        trip_ended=_end_trip_succeeded(messages)
        and read(f"USER#{ledger_id}", "TRIP#ACTIVE") is None,
        csv_bytes=_end_trip_csv(messages),
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
                    content=END_TRIP_CANCELLED,
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
