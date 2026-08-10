"""Telegram message and callback query handlers for the expenses bot."""

import asyncio
import logging

from langchain_core.messages import HumanMessage, ToolMessage
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.bot.agent.graph import build_graph

logger = logging.getLogger(__name__)

_graph = build_graph()
_END_TRIP_CONFIRM = "end_trip:confirm"
_END_TRIP_CANCEL = "end_trip:cancel"


def _config(telegram_user_id: str) -> dict:
    return {"configurable": {"thread_id": telegram_user_id}}


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
        await update.message.reply_text(last_msg.content or "(no reply)")
        await _send_charts(update, result)


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
        result = await asyncio.to_thread(_graph.invoke, None, config)
        last_msg = result["messages"][-1]
        await query.edit_message_text(last_msg.content or "Trip ended.")
        await _send_charts(update, result)
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
        await query.edit_message_text(last_msg.content or "Trip ending cancelled.")


async def _send_charts(update: Update, result: dict) -> None:
    """Send pie and bar chart images from the graph result if present.

    Args:
        update: The Telegram Update, used to find the chat to reply to.
        result: The graph result dict, checked for pie_chart_bytes and bar_chart_bytes.
    """
    msg = update.message or (update.callback_query and update.callback_query.message)
    if not msg:
        return
    pie = result.get("pie_chart_bytes")
    bar = result.get("bar_chart_bytes")
    if pie:
        await msg.reply_photo(pie)
    if bar:
        await msg.reply_photo(bar)
