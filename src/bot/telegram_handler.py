"""Telegram message and callback query handlers for the expenses bot."""

import asyncio
import io
import logging
import threading
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.bot.agent.graph import END_TRIP_NODE, build_graph, clear_thread_history
from src.bot.charts import generate_charts, generate_csv
from src.bot.storage.dynamodb import query_by_prefix
from src.bot.tools.fx import get_sgd_exchange_rates

logger = logging.getLogger(__name__)

_graph = build_graph()
_END_TRIP_CONFIRM = "end_trip:confirm"
_END_TRIP_CANCEL = "end_trip:cancel"

# Fragment of the Telegram Bot API error returned when an edit would leave the message
# unchanged. Matched on text because the API exposes no distinct error code for it.
_MESSAGE_NOT_MODIFIED = "not modified"

_CSV_FILENAME = "expenses.csv"

# matplotlib's pyplot keeps global figure state, so two trips ending at once must not
# render charts concurrently.
_CHART_LOCK = threading.Lock()


def _config(telegram_user_id: str) -> RunnableConfig:
    """Build the graph config that scopes checkpointed state to one Telegram user."""
    return {"configurable": {"thread_id": telegram_user_id}}


def _parse_mode(text: str) -> str | None:
    """Return 'HTML' if the text contains HTML tags, None otherwise."""
    return "HTML" if "<" in text else None


def _extract_text(content: str | list[Any]) -> str:
    """Extract plain text from an AI message content field.

    LangChain+Bedrock returns a list of typed blocks when the response contains
    both text and tool_use blocks. This function normalises that to a plain string.

    Args:
        content: Either a plain string or a list of Bedrock content blocks.

    Returns:
        The concatenated text from all text-type blocks, or the original string.
    """
    if isinstance(content, str):
        return content
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(texts)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process an incoming Telegram text message through the agent graph.

    Extracts the user ID, message date, and text from the Update, invokes the
    agent graph, and sends the reply. If the graph interrupts before end_trip_node,
    sends an inline Yes/No confirmation keyboard instead of the agent reply.

    Args:
        update: The incoming Telegram Update.
        context: The PTB handler context (unused).

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB operation fails.
    """
    if not update.message or not update.message.text or not update.effective_user:
        return

    user_id = str(update.effective_user.id)
    message_date = update.message.date.strftime("%Y-%m-%d")
    config = _config(user_id)

    state = await asyncio.to_thread(_graph.get_state, config)
    if END_TRIP_NODE in (state.next or ()):
        await update.message.reply_text(
            "Please confirm or cancel the pending trip ending first."
        )
        return

    result = await asyncio.to_thread(
        _graph.invoke,
        {
            "messages": [HumanMessage(content=update.message.text)],
            "telegram_user_id": user_id,
            "message_date": message_date,
        },
        config,
    )

    state_after = await asyncio.to_thread(_graph.get_state, config)
    if END_TRIP_NODE in (state_after.next or ()):
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Yes, end trip", callback_data=_END_TRIP_CONFIRM
                    ),
                    InlineKeyboardButton("No, cancel", callback_data=_END_TRIP_CANCEL),
                ]
            ]
        )
        await update.message.reply_text(
            "Are you sure you want to end the trip? All expenses will be deleted.",
            reply_markup=keyboard,
        )
    else:
        last_msg = result["messages"][-1]
        content = _extract_text(last_msg.content) or "(no reply)"
        await update.message.reply_text(content, parse_mode=_parse_mode(content))


async def _claim_confirmation(query: CallbackQuery) -> bool:
    """Remove the inline keyboard, claiming the pending confirmation for this caller.

    Telegram rejects an edit that would leave a message unchanged, so of two concurrent
    taps on the same keyboard exactly one succeeds in removing it. That makes this edit
    the lock on the confirmation, without the handler holding any state of its own.

    Args:
        query: The callback query whose message carries the inline keyboard.

    Returns:
        True if this call removed the keyboard and therefore owns the confirmation,
        False if the keyboard had already been removed by another tap.

    Raises:
        telegram.error.TelegramError: If the edit fails for any reason other than the
            message already being unmodified.
    """
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest as exc:
        if _MESSAGE_NOT_MODIFIED in str(exc).lower():
            return False
        raise
    return True


def _render_attachments(
    expenses: list[dict[str, Any]], telegram_user_id: str
) -> tuple[bytes | None, bytes | None, bytes | None]:
    """Render the trip's chart images and CSV file from live expense data.

    Must run before the graph is resumed, because end_trip deletes the expenses. Blocking
    throughout (HTTP plus matplotlib), so callers should run it in a worker thread.

    Failures degrade to a missing attachment rather than aborting the trip end: the
    authoritative export is the CSV that end_trip returns to the agent, not these files.
    When rates are unavailable the CSV is still produced, with a blank amount_sgd column,
    so the user keeps their data; charts are skipped because they plot SGD only.

    Args:
        expenses: Expense items for the trip, as returned by query_by_prefix.
        telegram_user_id: Used only to correlate log records.

    Returns:
        Tuple of (pie_chart_png, bar_chart_png, csv_bytes). Any element is None when that
        artefact could not be produced, and all three are None for a trip with no
        expenses.
    """
    if not expenses:
        return None, None, None

    fx_rates: dict[str, float] = {}
    try:
        fx_rates = get_sgd_exchange_rates()
    except (httpx.HTTPError, RuntimeError, ValidationError):
        logger.exception(
            "FX rate fetch failed for user %s; sending CSV without SGD column",
            telegram_user_id,
        )

    try:
        csv_bytes: bytes | None = generate_csv(expenses, fx_rates)
    except Exception:
        logger.exception("CSV generation failed for user %s", telegram_user_id)
        csv_bytes = None

    pie_bytes: bytes | None = None
    bar_bytes: bytes | None = None
    if fx_rates:
        try:
            with _CHART_LOCK:
                pie_bytes, bar_bytes = generate_charts(expenses, fx_rates)
        except Exception:
            logger.exception("Chart generation failed for user %s", telegram_user_id)

    return pie_bytes, bar_bytes, csv_bytes


async def _send_attachments(
    query: CallbackQuery,
    pie_bytes: bytes | None,
    bar_bytes: bytes | None,
    csv_bytes: bytes | None,
) -> None:
    """Send the trip's charts and CSV file as replies to the confirmation message.

    Args:
        query: The callback query whose message the attachments reply to.
        pie_bytes: Pie chart PNG, or None to skip it.
        bar_bytes: Bar chart PNG, or None to skip it.
        csv_bytes: CSV file contents, or None to skip it.

    Raises:
        telegram.error.TelegramError: If Telegram rejects an upload.
    """
    message = query.message
    if not isinstance(message, Message):
        logger.warning(
            "Callback message is unavailable; trip attachments were not sent."
        )
        return

    if pie_bytes:
        await message.reply_photo(pie_bytes)
    if bar_bytes:
        await message.reply_photo(bar_bytes)
    if csv_bytes:
        await message.reply_document(io.BytesIO(csv_bytes), filename=_CSV_FILENAME)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process the Yes/No inline keyboard response for ending a trip.

    Resumes the graph after an end_trip_node interrupt. The keyboard is removed first so
    a duplicate tap cannot resume the graph twice.

    On confirm, the attachments are rendered from live data and then the graph is resumed,
    which runs the end_trip tool — the tool performs the export and the deletion and
    returns the CSV that the agent turns into a summary. On cancel, a ToolMessage is
    injected so the agent knows the action was cancelled, then its reply is sent.

    Args:
        update: The incoming Telegram Update containing a callback_query.
        context: The PTB handler context (unused).

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB operation fails.
        telegram.error.TelegramError: If a Telegram API call fails.
    """
    query = update.callback_query
    if not query or not update.effective_user:
        return

    await query.answer()

    user_id = str(update.effective_user.id)

    # Claim before touching the graph: python-telegram-bot processes updates
    # concurrently, so a second tap can otherwise pass the state check below while this
    # one is still running and resume end_trip twice.
    if not await _claim_confirmation(query):
        logger.info("Ignoring duplicate end_trip confirmation from user %s", user_id)
        return

    config = _config(user_id)

    state = await asyncio.to_thread(_graph.get_state, config)
    if END_TRIP_NODE not in (state.next or ()):
        await query.edit_message_text("This confirmation has already been processed.")
        return

    if query.data == _END_TRIP_CONFIRM:
        # Render the attachments first: resuming the graph runs end_trip, which deletes
        # the expenses, so reading them afterwards would silently produce empty charts.
        expenses = await asyncio.to_thread(
            query_by_prefix, f"USER#{user_id}", "EXPENSE#"
        )
        pie_bytes, bar_bytes, csv_bytes = await asyncio.to_thread(
            _render_attachments, expenses, user_id
        )

        result = await asyncio.to_thread(_graph.invoke, None, config)
        content = _extract_text(result["messages"][-1].content) or "Trip ended."
        await query.edit_message_text(content, parse_mode=_parse_mode(content))
        await _send_attachments(query, pie_bytes, bar_bytes, csv_bytes)

        # Only once the summary and files are delivered: this discards the history the
        # summary was written from, and the next trip starts with a clean thread.
        await asyncio.to_thread(clear_thread_history, _graph, user_id)
    else:
        last_ai = state.values["messages"][-1]
        tool_call_id = last_ai.tool_calls[0]["id"]
        await asyncio.to_thread(
            _graph.update_state,
            config,
            {
                "messages": [
                    ToolMessage(
                        content="User cancelled ending the trip.",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
            END_TRIP_NODE,
        )
        result = await asyncio.to_thread(_graph.invoke, None, config)
        last_msg = result["messages"][-1]
        content = _extract_text(last_msg.content) or "Trip ending cancelled."
        await query.edit_message_text(content, parse_mode=_parse_mode(content))
