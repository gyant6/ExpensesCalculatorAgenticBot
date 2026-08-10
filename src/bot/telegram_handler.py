"""Telegram message and callback query handlers for the expenses bot."""

import asyncio
import logging

from langchain_core.messages import HumanMessage, ToolMessage
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.bot.agent.graph import build_graph
from src.bot.charts import generate_charts
from src.bot.storage.dynamodb import query_by_prefix
from src.bot.tools.fx import get_sgd_exchange_rates

logger = logging.getLogger(__name__)

_graph = build_graph()
_END_TRIP_CONFIRM = "end_trip:confirm"
_END_TRIP_CANCEL = "end_trip:cancel"


def _config(telegram_user_id: str) -> dict:
    return {"configurable": {"thread_id": telegram_user_id}}


def _parse_mode(text: str) -> str | None:
    """Return 'HTML' if the text contains HTML tags, None otherwise."""
    return "HTML" if "<" in text else None


def _extract_text(content: str | list) -> str:
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
    if "end_trip_node" in (state.next or ()):
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
    if "end_trip_node" in (state_after.next or ()):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, end trip", callback_data=_END_TRIP_CONFIRM),
            InlineKeyboardButton("No, cancel", callback_data=_END_TRIP_CANCEL),
        ]])
        await update.message.reply_text(
            "Are you sure you want to end the trip? All expenses will be deleted.",
            reply_markup=keyboard,
        )
    else:
        last_msg = result["messages"][-1]
        content = _extract_text(last_msg.content) or "(no reply)"
        await update.message.reply_text(content, parse_mode=_parse_mode(content))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process the Yes/No inline keyboard response for ending a trip.

    Resumes the graph after an end_trip_node interrupt. On confirm, executes
    end_trip and sends the agent's summary reply. On cancel, injects a ToolMessage
    so the agent knows the action was cancelled, then sends its follow-up reply.

    Args:
        update: The incoming Telegram Update containing a callback_query.
        context: The PTB handler context (unused).

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB operation fails.
    """
    query = update.callback_query
    if not query or not update.effective_user:
        return

    await query.answer()

    user_id = str(update.effective_user.id)
    config = _config(user_id)

    state = await asyncio.to_thread(_graph.get_state, config)
    if "end_trip_node" not in (state.next or ()):
        await query.edit_message_text("This confirmation has already been processed.")
        return

    if query.data == _END_TRIP_CONFIRM:
        # Capture the AI summary before resuming — end_trip_node routes to END and
        # deletes all expense data, so the pre-interrupt message is the one to show.
        summary_content = _extract_text(state.values["messages"][-1].content) or "Trip ended."
        pie_bytes, bar_bytes = await _generate_trip_charts(user_id)
        await asyncio.to_thread(_graph.invoke, None, config)
        await query.edit_message_text(summary_content, parse_mode=_parse_mode(summary_content))
        if pie_bytes:
            await query.message.reply_photo(pie_bytes)
        if bar_bytes:
            await query.message.reply_photo(bar_bytes)
    else:
        last_ai = state.values["messages"][-1]
        tool_call_id = last_ai.tool_calls[0]["id"]
        await asyncio.to_thread(
            _graph.update_state,
            config,
            {"messages": [ToolMessage(
                content="User cancelled ending the trip.",
                tool_call_id=tool_call_id,
            )]},
            "end_trip_node",
        )
        result = await asyncio.to_thread(_graph.invoke, None, config)
        last_msg = result["messages"][-1]
        content = _extract_text(last_msg.content) or "Trip ending cancelled."
        await query.edit_message_text(content, parse_mode=_parse_mode(content))


async def _generate_trip_charts(user_id: str) -> tuple[bytes | None, bytes | None]:
    """Fetch expenses and live FX rates, then generate pie and bar charts.

    Must be called BEFORE the graph resumes end_trip_node, because that node
    deletes all expense records from DynamoDB.

    Args:
        user_id: Telegram user ID string, used as the DynamoDB partition key suffix.

    Returns:
        Tuple of (pie_chart_bytes, bar_chart_bytes). Either or both may be None if
        there are no expenses or if chart generation fails.
    """
    try:
        expenses = await asyncio.to_thread(query_by_prefix, f"USER#{user_id}", "EXPENSE#")
        if not expenses:
            return None, None
        fx_rates = await get_sgd_exchange_rates()
        pie_bytes, bar_bytes = generate_charts(expenses, fx_rates)
        return pie_bytes, bar_bytes
    except Exception:
        logger.exception("Failed to generate trip charts for user %s", user_id)
        return None, None
