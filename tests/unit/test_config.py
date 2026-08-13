from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from src.bot.config import Settings


def build_settings(log_level: str) -> Settings:
    """Construct Settings with every required field supplied explicitly.

    Values are passed as keyword arguments rather than relying on a .env file, so the
    tests behave identically locally and in CI where no .env exists.

    Args:
        log_level: The LOG_LEVEL value under test.

    Returns:
        A populated Settings instance.
    """
    return Settings(
        ENVIRONMENT="test",
        TELEGRAM_BOT_TOKEN="token",
        AWS_BEDROCK_MODEL_ID="model",
        AWS_REGION="ap-southeast-1",
        LOG_LEVEL=log_level,
        DYNAMODB_TABLE_NAME="table",
    )


@pytest.mark.parametrize("supplied", ["info", "INFO", "Info", "  info  "])
def test_log_level_is_normalised_to_uppercase(supplied: str) -> None:
    assert build_settings(supplied).LOG_LEVEL == "INFO"


@pytest.mark.parametrize("supplied", ["debug", "warning", "error", "critical"])
def test_normalised_log_level_is_accepted_by_logging(supplied: str) -> None:
    """The normalised value must be usable directly in logging.basicConfig."""
    level = build_settings(supplied).LOG_LEVEL
    assert level in logging.getLevelNamesMapping()


@pytest.mark.parametrize("supplied", ["verbose", "trace", "", "1nfo"])
def test_log_level_rejects_unknown_value(supplied: str) -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL must be one of"):
        build_settings(supplied)
