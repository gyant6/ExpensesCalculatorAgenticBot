"""Entry point for running the Telegram bot in polling mode (local development).

For production, the Lambda handler receives updates via webhook instead.

Usage:
    uv run python -m src.bot.main
"""

import logging

from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters

from src.bot.config import settings
from src.bot.telegram_handler import handle_callback, handle_message

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    """Build the PTB application, register handlers, and start polling."""
    app = ApplicationBuilder().token(settings.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^end_trip:"))

    logger.info("Starting bot in polling mode.")
    app.run_polling()


if __name__ == "__main__":
    main()
