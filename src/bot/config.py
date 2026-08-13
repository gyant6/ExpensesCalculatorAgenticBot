"""Global application settings loaded via pydantic-settings.

Reads from a local `.env` file when present (local dev), or from process
environment variables (Lambda). All fields are required except
DYNAMODB_ENDPOINT_URL, which defaults to None in prod so boto3 connects
to real DynamoDB automatically.
"""

import logging

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    ENVIRONMENT: str
    TELEGRAM_BOT_TOKEN: str
    AWS_BEDROCK_MODEL_ID: str
    AWS_BEDROCK_PROFILE: str | None = None
    AWS_REGION: str
    LOG_LEVEL: str
    DYNAMODB_TABLE_NAME: str
    DYNAMODB_ENDPOINT_URL: str | None = None

    @field_validator("LOG_LEVEL")
    @classmethod
    def normalise_log_level(cls, value: str) -> str:
        """Normalise LOG_LEVEL to a level name the logging module accepts.

        `logging.basicConfig(level=...)` accepts only the canonical uppercase names, so a
        lowercase value kills the process at import with a bare `ValueError` raised from
        inside the logging module, naming neither the setting nor the file it came from.
        Validating here fails at settings load instead, reporting the offending value and
        the permitted ones.

        Args:
            value: Raw LOG_LEVEL as read from the environment or the .env file.

        Returns:
            The upper-cased, whitespace-stripped level name.

        Raises:
            ValueError: If the value is not a level name known to the logging module.
                pydantic wraps this in a ValidationError.
        """
        level = value.strip().upper()
        valid_levels = logging.getLevelNamesMapping()
        if level not in valid_levels:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(valid_levels)}, got {value!r}"
            )
        return level


settings = Settings()
