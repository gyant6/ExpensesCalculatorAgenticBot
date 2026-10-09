"""Structured logging: every log line is one JSON object, with secrets redacted.

CloudWatch Logs Insights discovers the fields of a JSON log event automatically, so a
query can filter on any of them — `filter chat = "123" and outcome = "error"` — with no
parsing. Lambda's own START, END and REPORT lines stay plain text; they come from the
platform, not from this formatter.

Structured fields are attached through `extra=log_fields(...)`:

    logger.info("turn", extra=log_fields(chat=ledger_id, outcome="replied"))
"""

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Final

REDACTED: Final = "***REDACTED***"

# The record attribute log_fields stores the fields under. One attribute holding a dict,
# rather than each field as its own attribute, so a field can never collide with one of
# LogRecord's own attributes (`name`, `msg`, `args`...), which logging refuses to set.
_FIELDS_ATTR: Final = "fields"

# Third-party loggers held above INFO regardless of LOG_LEVEL. httpx logs the full
# request URL at INFO, so every reply would otherwise write a line containing the token —
# redacted by the formatter, but there is no reason to emit it at all. The checkpointer
# logs one line per chunk written, burying everything else.
_NOISY_LOGGERS: Final = ("httpx", "httpcore", "langgraph_checkpoint_aws")


def log_fields(**fields: Any) -> dict[str, dict[str, Any]]:
    """Build the `extra=` argument that attaches structured fields to a log record.

    Args:
        **fields: Field names and values. Values that are not JSON types are written
            as their str().

    Returns:
        The mapping to pass as `extra=`.
    """
    return {_FIELDS_ATTR: fields}


class JsonFormatter(logging.Formatter):
    """Render each record as one line of JSON, with the given secrets replaced.

    Each line carries `timestamp` (UTC, ISO 8601), `level`, `logger` and `message`, the
    Lambda request id when the runtime supplies one, `exception` when the record has
    one, and every field from `log_fields`. Those base keys win over a field of the same
    name, so a field can never disguise a line's level or time.

    The Telegram Bot API carries the token in the URL path, so any library that logs a
    request URL, or any traceback from a failed call, contains it. Secrets are therefore
    replaced in the finished line, so they are caught wherever they appear:
    in the message, in a field, or in exception text — which is rendered here, out of
    reach of a logging.Filter. The bot token and webhook secret consist of characters
    JSON never escapes, so the encoded line contains them verbatim.
    """

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        """Create a formatter.

        Args:
            secrets: Values to replace with REDACTED wherever they appear. Empty and
                None entries are ignored, so optional settings can be passed as they are.
        """
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a JSON line.

        Args:
            record: The record to render.

        Returns:
            A single line of JSON, with every secret replaced by REDACTED.
        """
        entry: dict[str, Any] = dict(getattr(record, _FIELDS_ATTR, None) or {})
        entry.update(
            timestamp=datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            level=record.levelname,
            logger=record.name,
            message=record.getMessage(),
        )
        # Added to each record by the Lambda runtime's own logging filter; absent when
        # polling locally.
        request_id = getattr(record, "aws_request_id", None)
        if request_id:
            entry["request_id"] = request_id
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)

        # ensure_ascii=False keeps emoji and non-Latin text readable in CloudWatch rather
        # than as \u escapes; CloudWatch stores UTF-8.
        rendered = json.dumps(entry, default=str, ensure_ascii=False)
        for secret in self._secrets:
            rendered = rendered.replace(secret, REDACTED)
        return rendered


def configure_logging(level: str, secrets: Iterable[str | None]) -> None:
    """Send every log record through JsonFormatter at the given level.

    `logging.basicConfig` does nothing at all — not even set the level — when the root
    logger already has a handler, and the Lambda runtime attaches one before the
    application is imported. Under polling basicConfig is what creates the handler; in
    Lambda it is a no-op, and the explicit setLevel is the only thing that applies the
    level. Without it the root logger stays at the runtime's default of WARNING and every
    logger.info is silently dropped, the per-turn record included.

    Args:
        level: A level name logging accepts, e.g. "INFO".
        secrets: Values to redact from every line; None entries are ignored.
    """
    logging.basicConfig(level=level)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = JsonFormatter(secrets)
    for handler in root.handlers:
        handler.setFormatter(formatter)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
