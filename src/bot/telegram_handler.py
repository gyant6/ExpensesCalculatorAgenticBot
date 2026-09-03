"""Telegram message and callback query handlers for the expenses bot."""

import asyncio
import io
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from botocore.exceptions import ClientError
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
from src.bot.auth import (
    AUTH_PK_PREFIX,
    AUTH_SK,
    REVIEW_STATUS,
    AuthCommand,
    AuthStatus,
    EntityType,
)
from src.bot.charts_client import render_charts
from src.bot.config import settings
from src.bot.export import generate_csv
from src.bot.storage.dynamodb import (
    delete_item,
    get_item,
    put_item,
    query_by_prefix,
    scan_by_pk_prefix,
    update_item,
)
from src.bot.tools.fx import get_sgd_exchange_rates

logger = logging.getLogger(__name__)

_graph = build_graph()
_END_TRIP_CONFIRM = "end_trip:confirm"
_END_TRIP_CANCEL = "end_trip:cancel"
# Inline-keyboard callback data for the admin's Approve/Reject buttons, built from the
# same members as the typed /auth subcommands so the two cannot disagree.
_AUTH_CALLBACK_NAMESPACE = "auth"
_AUTH_APPROVE_PREFIX = f"{_AUTH_CALLBACK_NAMESPACE}:{AuthCommand.APPROVE}:"
_AUTH_REJECT_PREFIX = f"{_AUTH_CALLBACK_NAMESPACE}:{AuthCommand.REJECT}:"

# Built from AuthCommand so a new subcommand cannot be added without appearing in the
# usage text. LIST is the only member that takes no ID argument.
_AUTH_USAGE = "Usage:\n" + "\n".join(
    f"/auth {command}" if command is AuthCommand.LIST else f"/auth {command} <id>"
    for command in AuthCommand
)

# Fragments of Telegram Bot API errors. Matched on text because the API exposes no
# distinct error codes for them.
_MESSAGE_NOT_MODIFIED = "not modified"
_QUERY_EXPIRED = "query is too old"

# Shown in place of the confirmation keyboard while the trip is being ended, then
# overwritten with the summary. Ending a trip takes several seconds — an FX fetch, chart
# rendering, and two model round trips — so the tap needs visible acknowledgement.
_ENDING_TRIP_NOTICE = "Ending your trip and putting your summary together…"

_CSV_FILENAME = "expenses.csv"

# Chat types whose ledger is shared by everyone in the conversation.
_GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})

# Shown when the graph completes with no text to send. The tools have usually already run
# by this point, so the turn succeeded even though there is nothing to report — the
# wording says that rather than implying a fault.
_EMPTY_REPLY_FALLBACK = "Done."


@contextmanager
def _timed(timings: dict[str, int], phase: str) -> Iterator[None]:
    """Record how long a block took, in milliseconds, under the given phase name.

    Args:
        timings: Accumulator for one turn; the phase is added on exit.
        phase: Key to record the duration under, e.g. "graph".

    Yields:
        None. The block runs inside the measurement.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[phase] = round((time.perf_counter() - start) * 1000)


def _log_timings(
    turn: str,
    ledger_id: str,
    timings: dict[str, int],
    **context: int,
) -> None:
    """Emit one line summarising where a turn spent its time.

    A single line per turn rather than one per phase: the volume is then the same as no
    instrumentation, while still carrying every phase. Logged at INFO because latency is
    operational data wanted in production, where raising the log level to read it would
    mean reconfiguring a running function.

    Args:
        turn: Which handler produced the timings, e.g. "message".
        ledger_id: The user the turn belongs to, for correlation.
        timings: Phase name to duration in milliseconds; each is suffixed `_ms`.
        **context: Counts or other non-duration fields, logged under their own names so
            they are not mistaken for milliseconds.
    """
    fields = [f"{phase}_ms={ms}" for phase, ms in timings.items()]
    fields += [f"{name}={value}" for name, value in context.items()]
    logger.info("timing turn=%s user=%s %s", turn, ledger_id, " ".join(fields))


def _ledger_id_for(update: Update) -> str | None:
    """Return the ledger a message or callback belongs to.

    A group's expenses belong to the group, not to whichever member typed them: everyone
    contributes to one trip and sees one list. A private chat's ledger is the user. This
    is also the identity the auth gate approves, so authorisation and storage cannot
    disagree about who owns a trip.

    Args:
        update: The incoming update, from either handler.

    Returns:
        The ledger ID, or None if the update carries no chat to scope by.
    """
    chat = update.effective_chat
    if chat is None:
        return None
    # Telegram gives a private chat the same ID as the user it belongs to, so the chat ID
    # is the ledger in both cases — one expression, no branch to keep in step.
    return str(chat.id)


def _config(ledger_id: str) -> RunnableConfig:
    """Build the graph config that scopes checkpointed state to one Telegram user."""
    return {"configurable": {"thread_id": ledger_id}}


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


def _log_empty_reply(ledger_id: str, message: Any) -> None:
    """Record the shape of a final message that yielded no text.

    Seen once in production: the tool ran and the expense was stored, but the turn ended
    with nothing to send. Two causes are indistinguishable from the outside — the model
    returned no text, or `_extract_text` discarded blocks whose shape it does not
    recognise, which would be the more serious of the two and is currently silent.

    Logs the block types and the message class rather than the content itself, which can
    hold the user's expense text.

    Args:
        ledger_id: The user whose turn produced no text, for correlation.
        message: The final message from the graph, whatever type it turned out to be.
    """
    content = getattr(message, "content", None)
    if isinstance(content, list):
        shape: object = [
            sorted(block) if isinstance(block, dict) else type(block).__name__
            for block in content
        ]
    else:
        shape = type(content).__name__
    logger.warning(
        "Empty reply for user %s: message=%s blocks=%s tool_calls=%s",
        ledger_id,
        type(message).__name__,
        shape,
        len(getattr(message, "tool_calls", []) or []),
    )


async def _check_auth(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    auth_id: str,
    entity_type: EntityType,
    display_name: str,
) -> bool:
    """Check whether a user or group is authorised to use the bot.

    On first contact, creates a PENDING record in DynamoDB and sends an Approve/Reject
    keyboard to the admin. On subsequent contacts, returns immediately based on the
    stored status without hitting the admin again.

    Args:
        update: The incoming Update, used to reply to the requester.
        context: The PTB context, used to send the admin notification.
        auth_id: The Telegram user ID (positive) or group chat ID (negative) being checked.
        entity_type: Whether auth_id identifies a person or a group chat.
        display_name: Username or group title shown in the admin notification.

    Returns:
        True if the entity's status is APPROVED, False for PENDING, REJECTED, or new.

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB operation fails.
        telegram.error.TelegramError: If sending the admin notification fails.
    """
    auth_item = await asyncio.to_thread(get_item, f"{AUTH_PK_PREFIX}{auth_id}", AUTH_SK)

    if auth_item is None:
        now = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(
            put_item,
            {
                "PK": f"{AUTH_PK_PREFIX}{auth_id}",
                "SK": AUTH_SK,
                "status": AuthStatus.PENDING,
                "entity_type": entity_type,
                "username": display_name,
                "requested_at": now,
            },
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Approve",
                        callback_data=f"{_AUTH_APPROVE_PREFIX}{auth_id}",
                    ),
                    InlineKeyboardButton(
                        "Reject",
                        callback_data=f"{_AUTH_REJECT_PREFIX}{auth_id}",
                    ),
                ]
            ]
        )
        await context.bot.send_message(
            chat_id=settings.ADMIN_TELEGRAM_ID,
            text=f"Access request from {display_name} ({entity_type} ID: {auth_id})",
            reply_markup=keyboard,
        )
        if update.message:
            await update.message.reply_text(
                "You don't have access yet. A request has been sent for approval."
            )
        return False

    if auth_item.get("status") == AuthStatus.APPROVED:
        return True
    if update.message:
        await update.message.reply_text(
            "You don't have access yet. A request has been sent for approval."
        )
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process an incoming Telegram text message through the agent graph.

    Runs the auth gate first. APPROVED entities proceed through the graph; others are
    replied to or silently ignored according to their status. If the graph interrupts
    before end_trip_node, sends an inline Yes/No confirmation keyboard instead of the
    agent reply.

    Args:
        update: The incoming Telegram Update.
        context: The PTB handler context.

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB operation fails.
    """
    if not update.message or not update.message.text or not update.effective_user:
        return

    user_id = str(update.effective_user.id)
    chat = update.effective_chat

    # The ledger is the chat, not the sender. In a group everyone contributes to one trip
    # and sees one expense list, which is the point of adding the bot to a group at all —
    # scoping by sender gave each member a private trip inside a shared conversation, so
    # one person's expenses were invisible to everyone else. It also matches the auth
    # model, which already approves a group as a single entity rather than per member.
    if chat and chat.type in _GROUP_CHAT_TYPES:
        ledger_id = str(chat.id)
        entity_type = EntityType.GROUP
        display_name = chat.title or ledger_id
    else:
        ledger_id = user_id
        entity_type = EntityType.USER
        raw_username = update.effective_user.username
        display_name = (
            f"@{raw_username}"
            if raw_username
            else (update.effective_user.full_name or user_id)
        )

    timings: dict[str, int] = {}

    with _timed(timings, "auth"):
        authorised = await _check_auth(
            update, context, ledger_id, entity_type, display_name
        )
    if not authorised:
        return

    message_date = update.message.date.strftime("%Y-%m-%d")
    config = _config(ledger_id)

    with _timed(timings, "state"):
        state = await asyncio.to_thread(_graph.get_state, config)
    if END_TRIP_NODE in (state.next or ()):
        await update.message.reply_text(
            "Please confirm or cancel the pending trip ending first."
        )
        return

    # Covers the Bedrock round trips, every tool's DynamoDB call, and a checkpoint write
    # per super-step. A dominant figure here needs breaking down further before it says
    # anything actionable.
    with _timed(timings, "graph"):
        result = await asyncio.to_thread(
            _graph.invoke,
            {
                "messages": [HumanMessage(content=update.message.text)],
                "ledger_id": ledger_id,
                "message_date": message_date,
            },
            config,
        )

    with _timed(timings, "state_after"):
        state_after = await asyncio.to_thread(_graph.get_state, config)

    with _timed(timings, "reply"):
        if END_TRIP_NODE in (state_after.next or ()):
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Yes, end trip", callback_data=_END_TRIP_CONFIRM
                        ),
                        InlineKeyboardButton(
                            "No, cancel", callback_data=_END_TRIP_CANCEL
                        ),
                    ]
                ]
            )
            await update.message.reply_text(
                "Are you sure you want to end the trip? All expenses will be deleted.",
                reply_markup=keyboard,
            )
        else:
            last_msg = result["messages"][-1]
            content = _extract_text(last_msg.content)
            if not content:
                _log_empty_reply(ledger_id, last_msg)
                content = _EMPTY_REPLY_FALLBACK
            await update.message.reply_text(content, parse_mode=_parse_mode(content))

    _log_timings("message", ledger_id, timings)


async def _acknowledge_callback(query: CallbackQuery) -> None:
    """Clear the client-side loading state on a tapped inline button.

    Telegram expires a callback query a short while after the tap, and updates are
    processed one at a time — so a second tap queued behind a trip end, which takes
    several seconds, reaches this call with an ID Telegram has already discarded. That is
    a duplicate tap rather than a fault, and the caller goes on to reject it properly at
    the confirmation claim.

    Args:
        query: The callback query to acknowledge.

    Raises:
        telegram.error.TelegramError: If the call fails for any reason other than the
            query having expired.
    """
    try:
        await query.answer()
    except BadRequest as exc:
        if _QUERY_EXPIRED in str(exc).lower():
            logger.info("Ignoring expired callback query; likely a repeated tap")
            return
        raise


async def _claim_confirmation(query: CallbackQuery) -> bool:
    """Remove the inline keyboard, claiming the pending confirmation for this caller.

    Telegram rejects an edit that would leave a message unchanged, so of two taps on the
    same keyboard exactly one succeeds in removing it. That makes this edit the lock on
    the confirmation, without the handler holding any state of its own.

    The claim must be the keyboard removal specifically, because that outcome is reached
    once and never again — the message ends with no keyboard whatever else happens to it.
    Claiming by writing known text instead is only exclusive while the message still
    holds that text: once the winning tap replaces it with the summary, a queued second
    tap writes the text successfully, claims a confirmation that is already finished, and
    overwrites the summary with a stale-confirmation notice.

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
    expenses: list[dict[str, Any]], ledger_id: str
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
        ledger_id: Used only to correlate log records.

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
            ledger_id,
        )

    try:
        csv_bytes: bytes | None = generate_csv(expenses, fx_rates)
    except Exception:
        logger.exception("CSV generation failed for user %s", ledger_id)
        csv_bytes = None

    pie_bytes: bytes | None = None
    bar_bytes: bytes | None = None
    if fx_rates:
        # render_charts logs and returns None on failure rather than raising: charts are
        # a convenience, and the trip summary must go out regardless.
        charts = render_charts(expenses, fx_rates)
        if charts is None:
            logger.warning("Charts unavailable for user %s", ledger_id)
        else:
            pie_bytes, bar_bytes = charts

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


async def handle_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process the Yes/No inline keyboard response for ending a trip.

    Resumes the graph after an end_trip_node interrupt. The keyboard is removed first so
    a duplicate tap cannot resume the graph twice.

    On confirm, the attachments are rendered from live data and then the graph is resumed,
    which runs the end_trip tool — the tool performs the export and the deletion and
    returns the CSV that the agent turns into a summary. On cancel, a ToolMessage is
    injected so the agent knows the action was cancelled, then its reply is sent.

    Args:
        update: The incoming Telegram Update containing a callback_query.
        _context: Unused.

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB operation fails.
        telegram.error.TelegramError: If a Telegram API call fails.
    """
    query = update.callback_query
    if not query or not update.effective_user:
        return

    await _acknowledge_callback(query)

    ledger_id = _ledger_id_for(update)
    if ledger_id is None:
        return

    # Claim before touching the graph, so a second tap cannot pass the state check below
    # and resume end_trip twice.
    if not await _claim_confirmation(query):
        logger.info("Ignoring duplicate end_trip confirmation for ledger %s", ledger_id)
        return

    config = _config(ledger_id)

    state = await asyncio.to_thread(_graph.get_state, config)
    if END_TRIP_NODE not in (state.next or ()):
        await query.edit_message_text("This confirmation has already been processed.")
        return

    if query.data == _END_TRIP_CONFIRM:
        timings: dict[str, int] = {}

        # Ending a trip takes several seconds — an FX fetch, chart rendering and two model
        # round trips — so say so rather than leaving the tap unacknowledged. This message
        # is edited again with the summary below, so nothing extra appears in the chat.
        await query.edit_message_text(_ENDING_TRIP_NOTICE)

        # Render the attachments first: resuming the graph runs end_trip, which deletes
        # the expenses, so reading them afterwards would silently produce empty charts.
        with _timed(timings, "query"):
            expenses = await asyncio.to_thread(
                query_by_prefix, f"USER#{ledger_id}", "EXPENSE#"
            )
        # FX fetch, CSV build and chart render together, since they share one call.
        with _timed(timings, "attachments"):
            pie_bytes, bar_bytes, csv_bytes = await asyncio.to_thread(
                _render_attachments, expenses, ledger_id
            )

        with _timed(timings, "graph"):
            result = await asyncio.to_thread(_graph.invoke, None, config)
        content = _extract_text(result["messages"][-1].content) or "Trip ended."

        with _timed(timings, "send"):
            await query.edit_message_text(content, parse_mode=_parse_mode(content))
            await _send_attachments(query, pie_bytes, bar_bytes, csv_bytes)

        # Only once the summary and files are delivered: this discards the history the
        # summary was written from, and the next trip starts with a clean thread.
        with _timed(timings, "clear"):
            await asyncio.to_thread(clear_thread_history, _graph, ledger_id)

        _log_timings("end_trip", ledger_id, timings, expenses=len(expenses))
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


async def handle_auth_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Process the admin's Approve/Reject decision for an access request.

    Triggered when the admin taps the inline keyboard sent when an unknown entity first
    contacts the bot. Updates the auth record in DynamoDB and notifies the requester.

    Callback data format: "auth:approve:<auth_id>" or "auth:reject:<auth_id>".
    auth_id is a positive integer for users and negative for groups, matching Telegram IDs.

    Args:
        update: The incoming Telegram Update containing a callback_query.
        context: The PTB handler context (used to notify the requester).

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB operation fails.
        telegram.error.TelegramError: If a Telegram API call fails.
    """
    query = update.callback_query
    if not query:
        return

    await _acknowledge_callback(query)

    data = query.data or ""
    if data.startswith(_AUTH_APPROVE_PREFIX):
        action = AuthCommand.APPROVE
        auth_id = data[len(_AUTH_APPROVE_PREFIX) :]
    elif data.startswith(_AUTH_REJECT_PREFIX):
        action = AuthCommand.REJECT
        auth_id = data[len(_AUTH_REJECT_PREFIX) :]
    else:
        return

    auth_item = await asyncio.to_thread(get_item, f"{AUTH_PK_PREFIX}{auth_id}", AUTH_SK)
    if not auth_item:
        await query.edit_message_text(
            "Auth record not found — it may have been removed."
        )
        return

    entity_type = auth_item.get("entity_type", EntityType.USER)
    display_name = auth_item.get("username", auth_id)
    now = datetime.now(timezone.utc).isoformat()
    new_status = REVIEW_STATUS[action]

    await asyncio.to_thread(
        update_item,
        f"{AUTH_PK_PREFIX}{auth_id}",
        AUTH_SK,
        {"status": new_status, "reviewed_at": now},
    )

    if new_status is AuthStatus.APPROVED:
        requester_text = "Your access has been approved! Start a new trip to start tracking your expenses!"
        admin_text = f"Approved: {display_name} ({entity_type} ID: {auth_id})"
    else:
        requester_text = "Your access request has been rejected."
        admin_text = f"Rejected: {display_name} ({entity_type} ID: {auth_id})"

    await context.bot.send_message(chat_id=int(auth_id), text=requester_text)
    await query.edit_message_text(admin_text)


async def handle_admin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /auth admin commands sent directly to the bot.

    PTB routes CommandHandlers separately from MessageHandlers, so this function is never
    reached via handle_message and the auth gate never runs for it. The admin ID check
    here is the sole guard.

    Subcommands (passed as context.args):
        list                — list all AUTH# records with status, type, name, and ID
        approve <id>        — set the record's status to APPROVED
        reject <id>         — set the record's status to REJECTED
        delete <id>         — delete the record so the entity can re-apply from scratch

    Args:
        update: The incoming Telegram Update.
        context: The PTB handler context; context.args holds the subcommand and arguments.

    Raises:
        botocore.exceptions.ClientError: If a DynamoDB operation fails (excluding
            ConditionalCheckFailedException on approve/reject, which is handled inline).
        telegram.error.TelegramError: If a Telegram API call fails.
    """
    if (
        not update.effective_user
        or update.effective_user.id != settings.ADMIN_TELEGRAM_ID
    ):
        return
    if not update.message:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(_AUTH_USAGE)
        return

    try:
        subcommand = AuthCommand(args[0])
    except ValueError:
        await update.message.reply_text(f"Unknown subcommand. {_AUTH_USAGE}")
        return

    if subcommand is AuthCommand.LIST:
        records = await asyncio.to_thread(scan_by_pk_prefix, AUTH_PK_PREFIX)
        if not records:
            await update.message.reply_text("No auth records found.")
            return
        lines = ["Status | Type | Name (ID)"]
        for r in sorted(records, key=lambda x: x.get("username", "")):
            auth_id = r["PK"].removeprefix(AUTH_PK_PREFIX)
            lines.append(
                f"{r.get('status', '?')} | {r.get('entity_type', '?')} | "
                f"{r.get('username', '?')} ({auth_id})"
            )
        await update.message.reply_text("\n".join(lines))
        return

    if len(args) < 2:
        await update.message.reply_text(f"Usage: /auth {subcommand} <id>")
        return
    auth_id = args[1]
    pk = f"{AUTH_PK_PREFIX}{auth_id}"

    if subcommand is AuthCommand.DELETE:
        await asyncio.to_thread(delete_item, pk, AUTH_SK)
        await update.message.reply_text(f"Deleted auth record for ID {auth_id}.")
        return

    new_status = REVIEW_STATUS[subcommand]
    now = datetime.now(timezone.utc).isoformat()
    try:
        await asyncio.to_thread(
            update_item, pk, AUTH_SK, {"status": new_status, "reviewed_at": now}
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            await update.message.reply_text(f"No auth record found for ID {auth_id}.")
            return
        raise
    await update.message.reply_text(f"Set {auth_id} to {new_status}.")
