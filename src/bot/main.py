"""Entry point for the Telegram bot.

Local development:  run `uv run python -m src.bot.main` — starts long-polling.
Production:         AWS Lambda calls `lambda_handler` directly via API Gateway webhook.
"""

import asyncio
import json
import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
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
    global _lambda_loop
    if _lambda_loop is None:
        _lambda_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_lambda_loop)

    body: dict[str, Any] = json.loads(event.get("body") or "{}")
    _lambda_loop.run_until_complete(_handle_update(body))
    return {"statusCode": 200, "body": "OK"}


# ── Polling (local development) ────────────────────────────────────────────────


def main() -> None:
    """Build the PTB application and start long-polling (local development only)."""
    app = _build_app()
    logger.info("Starting bot in polling mode.")
    app.run_polling()


if __name__ == "__main__":
    main()
