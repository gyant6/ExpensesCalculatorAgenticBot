"""One structured log entry per conversation turn: what was said, what the bot did, and
what it replied.

The conversation checkpoint is deleted when a trip ends, so after that these entries are
the only record of how the bot behaved — what to debug a failure from, and the source of
realistic test scenarios. CloudWatch keeps them for the log group's retention (60 days).

A turn is logged by the handler that ran it, after the reply is sent or the turn fails.
Messages the bot ignores are not turns and are never logged.
"""

import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from src.bot.logging_setup import log_fields

logger = logging.getLogger(__name__)

# Long enough for the whole expense list of a typical trip — the list the model read is
# what a wrong-row edit has to be diagnosed from — while a turn stays far below
# CloudWatch's 256 KB limit on a single log event.
TOOL_RESULT_MAX_CHARS: Final = 8_000

# Telegram's own limit on a message, so a user message is always logged whole. A longer
# reply is sent in chunks; its first part is what the log keeps.
TEXT_MAX_CHARS: Final = 4_096

_EVENT: Final = "turn"


class TurnKind(StrEnum):
    """What started the turn."""

    MESSAGE = "message"
    END_TRIP_CONFIRMED = "end_trip_confirmed"
    END_TRIP_CANCELLED = "end_trip_cancelled"


class TurnOutcome(StrEnum):
    """How the turn ended."""

    REPLIED = "replied"
    # The model called end_trip and the user was shown the Yes/No keyboard.
    CONFIRMATION_ASKED = "confirmation_asked"
    # The resume after "Yes" returned with the trip still active.
    END_TRIP_FAILED = "end_trip_failed"
    # An exception escaped the graph; the error handler logs the traceback separately.
    ERROR = "error"


def _truncate(text: str, limit: int) -> str:
    """The text cut to `limit` characters, saying how much was dropped."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[{len(text) - limit} more characters]"


def _steps(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """Each tool call in order, with its arguments and the result it returned.

    A call with no result — end_trip when the turn paused for confirmation — is listed
    without one. A result whose call was made in an earlier turn — end_trip's, after the
    user taps Yes, or the cancellation injected after No — is listed on its own, without
    arguments, so the turn that received it still shows what it was.
    """
    steps: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                step: dict[str, Any] = {"tool": call["name"], "args": call["args"]}
                steps.append(step)
                if call["id"]:
                    by_id[call["id"]] = step
        elif isinstance(message, ToolMessage):
            matched = by_id.get(message.tool_call_id)
            if matched is None:
                matched = {"tool": message.name}
                steps.append(matched)
            matched["result"] = _truncate(message.text, TOOL_RESULT_MAX_CHARS)
            if message.status != "success":
                matched["status"] = message.status
    return steps


def _token_totals(messages: Sequence[BaseMessage]) -> dict[str, int]:
    """Model calls in the turn and the tokens they used, as Bedrock reported them."""
    totals = {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
    }
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        totals["model_calls"] += 1
        usage = message.usage_metadata
        if usage is None:
            continue
        totals["input_tokens"] += usage["input_tokens"]
        totals["output_tokens"] += usage["output_tokens"]
        totals["cache_read_tokens"] += (usage.get("input_token_details") or {}).get(
            "cache_read", 0
        )
    return totals


def log_turn(
    *,
    kind: TurnKind,
    outcome: TurnOutcome,
    chat: str,
    sender: int | None,
    update_id: int,
    new_messages: Sequence[BaseMessage],
    reply: str | None,
    timings: Mapping[str, int],
    error: BaseException | None = None,
    **counts: int,
) -> None:
    """Write one structured log entry describing a turn.

    Logged at INFO, or WARNING when the turn failed, as a single line with the fields
    below; see logging_setup for the line format.

    Args:
        kind: What started the turn.
        outcome: How it ended.
        chat: The ledger (Telegram chat ID) the turn belongs to.
        sender: The Telegram user who sent the message or tapped the button, if known.
        update_id: Telegram's id for the update, matching the dedup marker.
        new_messages: The messages this turn added to the conversation, in order — the
            user's message, each model response and each tool result. Not the history
            before the turn.
        reply: The text sent to the chat, if any.
        timings: Phase name to milliseconds, as measured by the handler.
        error: The exception that ended the turn, for outcome ERROR.
        **counts: Further integer facts about the turn, e.g. expenses=12.

    Fields written: event ("turn"), kind, outcome, chat, sender, update_id,
    user_message, steps (tool, args, result and a non-success status per call), reply,
    model_calls, input_tokens, output_tokens, cache_read_tokens, timings_ms, error, and
    each count.
    """
    user_message = next(
        (m.text for m in new_messages if isinstance(m, HumanMessage)), None
    )
    failed = outcome in (TurnOutcome.ERROR, TurnOutcome.END_TRIP_FAILED)
    logger.log(
        logging.WARNING if failed else logging.INFO,
        "turn %s %s",
        kind,
        outcome,
        extra=log_fields(
            event=_EVENT,
            kind=str(kind),
            outcome=str(outcome),
            chat=chat,
            sender=sender,
            update_id=update_id,
            user_message=(
                _truncate(user_message, TEXT_MAX_CHARS) if user_message else None
            ),
            steps=_steps(new_messages),
            reply=_truncate(reply, TEXT_MAX_CHARS) if reply else None,
            **_token_totals(new_messages),
            timings_ms=dict(timings),
            error=f"{type(error).__name__}: {error}" if error else None,
            **counts,
        ),
    )
