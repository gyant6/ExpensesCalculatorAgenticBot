"""Tests for the JSON log formatter and logging configuration.

Production logs are queried by field in CloudWatch Logs Insights, which only discovers
fields when the whole line is JSON — so every line must parse, and secrets must be gone
from every part of it, including exception text.

The level must also be applied where basicConfig cannot apply it. The Lambda runtime
attaches a handler to the root logger before user code imports, and logging.basicConfig
is documented to do nothing when handlers are already present — it does not even set the
level. Relying on it alone left the root logger at the runtime's WARNING default in
production, silently dropping every logger.info in the application.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from src.bot.logging_setup import (
    REDACTED,
    JsonFormatter,
    configure_logging,
    log_fields,
)

SECRET = "123456:ABC-def_secret"


def _record(
    message: str = "hello %s",
    args: tuple[Any, ...] = ("world",),
    level: int = logging.INFO,
    exc_info: Any = None,
    extra: Mapping[str, Any] | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        "src.bot.test", level, __file__, 1, message, args, exc_info
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def _render(record: logging.LogRecord, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    line = JsonFormatter(secrets).format(record)
    assert "\n" not in line
    parsed: dict[str, Any] = json.loads(line)
    return parsed


def test_a_line_carries_the_base_keys() -> None:
    entry = _render(_record(level=logging.WARNING))

    assert entry["message"] == "hello world"
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "src.bot.test"
    assert entry["timestamp"].endswith("+00:00")


def test_fields_become_top_level_keys() -> None:
    entry = _render(
        _record(extra=log_fields(chat="42", steps=[{"tool": "add_expense"}]))
    )

    assert entry["chat"] == "42"
    assert entry["steps"] == [{"tool": "add_expense"}]


def test_a_field_cannot_override_a_base_key() -> None:
    entry = _render(
        _record(level=logging.ERROR, extra=log_fields(level="INFO", message="x"))
    )

    assert entry["level"] == "ERROR"
    assert entry["message"] == "hello world"


def test_values_that_are_not_json_types_are_written_as_text() -> None:
    entry = _render(
        _record(extra=log_fields(when=object.__new__(type("Thing", (), {}))))
    )

    assert "Thing object" in entry["when"]


def test_emoji_stay_readable() -> None:
    line = JsonFormatter().format(_record(extra=log_fields(reply="Woof! 🐾")))

    assert "🐾" in line


def test_the_lambda_request_id_is_included_when_present() -> None:
    assert _render(_record(extra={"aws_request_id": "req-1"}))["request_id"] == "req-1"
    assert "request_id" not in _render(_record())


def test_a_secret_is_redacted_from_message_fields_and_exception() -> None:
    try:
        raise RuntimeError(f"https://api.telegram.org/bot{SECRET}/sendMessage failed")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = _record(
        "calling %s",
        (SECRET,),
        exc_info=exc_info,
        extra=log_fields(url=f"/bot{SECRET}/getMe"),
    )

    line = JsonFormatter([SECRET, None, ""]).format(record)

    assert SECRET not in line
    entry = json.loads(line)
    assert entry["message"] == f"calling {REDACTED}"
    assert entry["url"] == f"/bot{REDACTED}/getMe"
    assert "RuntimeError" in entry["exception"]


@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    """The root logger with one handler, restored afterwards."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = [logging.StreamHandler()]
    yield root
    root.handlers, root.level = saved_handlers, saved_level


def test_basic_config_cannot_set_the_level_when_a_handler_exists() -> None:
    # The behaviour being defended against, pinned so it is visible rather than folklore.
    root = logging.getLogger("test_precondition")
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.WARNING)

    logging.basicConfig(level=logging.INFO)

    assert root.level == logging.WARNING


def test_configure_logging_applies_the_level_when_a_handler_exists(
    root_logger: logging.Logger,
) -> None:
    # As in Lambda: a handler already attached, and the runtime's WARNING default.
    root_logger.setLevel(logging.WARNING)

    configure_logging("INFO", secrets=[])

    assert root_logger.level == logging.INFO
    assert root_logger.isEnabledFor(logging.INFO)


def test_configure_logging_formats_every_root_handler_as_json(
    root_logger: logging.Logger,
) -> None:
    configure_logging("INFO", secrets=[SECRET])

    assert root_logger.level == logging.INFO
    formatter = root_logger.handlers[0].formatter
    assert isinstance(formatter, JsonFormatter)
    assert SECRET not in formatter.format(_record("token %s", (SECRET,)))


def test_configure_logging_quietens_the_chatty_libraries(
    root_logger: logging.Logger,
) -> None:
    configure_logging("DEBUG", secrets=[])

    assert logging.getLogger("httpx").level == logging.WARNING
