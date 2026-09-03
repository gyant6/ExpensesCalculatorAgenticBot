"""Entry point for the Telegram bot.

Local development:  run `uv run python -m src.bot.main` — starts long-polling.
Production:         AWS Lambda calls `lambda_handler` directly via API Gateway webhook.
"""

import asyncio
import hmac
import json
import logging
from typing import Any

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.bot.config import settings
from src.bot.telegram_handler import (
    handle_admin_command,
    handle_auth_callback,
    handle_callback,
    handle_message,
)

_REDACTED = "***REDACTED***"


class _RedactingFormatter(logging.Formatter):
    """Formatter that strips the bot token from every record it renders.

    The Telegram Bot API carries the token in the URL path, so any library that logs a
    request URL, or any traceback from a failed call, will contain it. Redacting at the
    formatter catches all of them at once — including exception text, which a
    logging.Filter cannot reach because the traceback is rendered here rather than
    stored on the record.
    """

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if settings.TELEGRAM_BOT_TOKEN:
            rendered = rendered.replace(settings.TELEGRAM_BOT_TOKEN, _REDACTED)
        return rendered


logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# basicConfig does nothing at all — not even set the level — when the root logger already
# has a handler, and the Lambda runtime attaches one before this module is imported. Under
# polling the call above is what configures logging; in Lambda it is a no-op and this line
# is the only thing that applies LOG_LEVEL. Without it the root logger stays at the
# runtime's default of WARNING and every logger.info in the application is silently
# dropped, including the per-turn timing line.
logging.getLogger().setLevel(settings.LOG_LEVEL)

for _handler in logging.getLogger().handlers:
    _handler.setFormatter(
        _RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )

# Third-party loggers held above INFO regardless of LOG_LEVEL. httpx logs the full
# request URL at INFO, so every reply would otherwise write a line containing the token —
# redacted by the formatter above, but there is no reason to emit it at all. The
# checkpointer logs one line per chunk written, dozens per turn, burying everything else.
for _noisy_logger in ("httpx", "httpcore", "langgraph_checkpoint_aws"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def _log_handler_error(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log any exception a handler let escape, and tell the user their turn failed.

    Without a registered error handler PTB logs the traceback and nothing else, so a
    failed turn is indistinguishable from a slow one: the user waits for a reply that is
    never coming, and in production the only trace is a stack in CloudWatch nobody is
    watching. This does not attempt recovery — the turn is already lost — it makes the
    failure visible on both ends.

    Args:
        update: The update being processed, or None if the failure was not update-bound.
        context: PTB context; `context.error` holds the exception.
    """
    logger.exception(
        "Unhandled error while processing an update", exc_info=context.error
    )

    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Something went wrong handling that. Please try again.",
        )
    except TelegramError:
        # The notification is best effort; failing to deliver it must not mask the
        # original error above, which is the one worth reading.
        logger.exception("Could not notify the user about the previous error")


def _build_app() -> Application:  # type: ignore[type-arg]
    """Build the PTB Application with all handlers registered.

    Shared by both polling (local) and webhook (Lambda) modes so handler
    registration is never duplicated.

    Returns:
        A configured but not yet initialised PTB Application.
    """
    app = ApplicationBuilder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("auth", handle_admin_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^end_trip:"))
    app.add_handler(CallbackQueryHandler(handle_auth_callback, pattern="^auth:"))
    app.add_error_handler(_log_handler_error)
    return app


# ── Lambda / webhook (production) ─────────────────────────────────────────────

# One app and one event loop per Lambda container, reused across warm invocations
# so PTB only calls getMe once per container lifetime rather than on every update.
_lambda_app: Application | None = None  # type: ignore[type-arg]
_lambda_loop: asyncio.AbstractEventLoop | None = None


async def _handle_update(update_data: dict[str, Any]) -> None:
    """Lazily initialise the PTB app then dispatch one update.

    Args:
        update_data: Parsed Telegram update JSON from the API Gateway event body.
    """
    global _lambda_app
    if _lambda_app is None:
        _lambda_app = _build_app()
        await _lambda_app.initialize()
        logger.info("PTB application initialised (cold start).")
    update = Update.de_json(update_data, _lambda_app.bot)
    await _lambda_app.process_update(update)


# Header Telegram echoes on every delivery when a secret_token was given to setWebhook.
# API Gateway lowercases header names in the payload-format-2.0 event.
_SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"

_FORBIDDEN = {"statusCode": 403, "body": "Forbidden"}
_OK = {"statusCode": 200, "body": "OK"}


def _is_authentic(event: dict[str, Any]) -> bool:
    """Check that a webhook delivery carries the secret token agreed with Telegram.

    The gateway URL is not a credential — anyone who learns it could otherwise POST a
    forged update naming the admin's Telegram ID and reach the /auth commands through it.
    This header is what makes a delivery provably Telegram's.

    Args:
        event: The API Gateway proxy event.

    Returns:
        True if the delivery carries the expected token. False if it is missing or wrong,
        or if no secret is configured at all — an unconfigured secret rejects everything
        rather than accepting everything, so a misdeploy fails closed.
    """
    expected = settings.WEBHOOK_SECRET
    if not expected:
        logger.error(
            "WEBHOOK_SECRET is not configured; rejecting the delivery. Set the SSM "
            "parameter and register the same value with setWebhook."
        )
        return False

    headers = event.get("headers") or {}
    presented = headers.get(_SECRET_TOKEN_HEADER)
    # compare_digest rather than == so the comparison does not leak the token's length or
    # its matching prefix through timing.
    return presented is not None and hmac.compare_digest(presented, expected)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point for production webhook mode.

    API Gateway forwards each Telegram webhook POST here. The update is
    processed synchronously and a 200 is returned to acknowledge receipt.
    Telegram retries updates that are not acknowledged within the timeout, so
    this function must complete before the Lambda timeout — ensure the Lambda
    timeout is set generously (≥ 30 s) to accommodate LLM and DynamoDB latency.

    Args:
        event: API Gateway proxy integration event. The Telegram update JSON is
            in event["body"] as a string.
        context: Lambda context object (unused).

    Returns:
        API Gateway response dict with statusCode 200.
    """
    if not _is_authentic(event):
        return _FORBIDDEN

    global _lambda_loop
    if _lambda_loop is None:
        _lambda_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_lambda_loop)

    body: dict[str, Any] = json.loads(event.get("body") or "{}")
    _lambda_loop.run_until_complete(_handle_update(body))
    return _OK


# ── Polling (local development) ────────────────────────────────────────────────


def main() -> None:
    """Build the PTB application and start long-polling (local development only)."""
    app = _build_app()
    logger.info("Starting bot in polling mode.")
    app.run_polling()


if __name__ == "__main__":
    main()
