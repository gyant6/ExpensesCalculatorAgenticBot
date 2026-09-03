"""Test that the log level is applied even where basicConfig cannot apply it.

The Lambda runtime attaches a handler to the root logger before user code imports, and
logging.basicConfig is documented to do nothing when handlers are already present — it
does not even set the level. Relying on it alone left the root logger at the runtime's
WARNING default in production, silently dropping every logger.info in the application.
"""

from __future__ import annotations

import logging

import pytest


def test_basic_config_cannot_set_the_level_when_a_handler_exists() -> None:
    # The behaviour being defended against, pinned so it is visible rather than folklore.
    root = logging.getLogger("test_precondition")
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.WARNING)

    logging.basicConfig(level=logging.INFO)

    assert root.level == logging.WARNING


def test_explicit_set_level_applies_regardless_of_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = logging.getLogger()
    original = root.level
    try:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.WARNING)

        # What main.py does after basicConfig.
        root.setLevel("INFO")

        assert root.level == logging.INFO
        assert root.isEnabledFor(logging.INFO)
    finally:
        root.setLevel(original)
