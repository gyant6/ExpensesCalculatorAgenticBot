"""Global application settings loaded via pydantic-settings.

Reads from a local `.env` file when present (local dev), or from process
environment variables (Lambda). All fields are required except
DYNAMODB_ENDPOINT_URL, which defaults to None in prod so boto3 connects
to real DynamoDB automatically.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    ENVIRONMENT: str
    TELEGRAM_BOT_TOKEN: str
    AWS_REGION: str
    AWS_BEDROCK_PROFILE: str | None = None
    LOG_LEVEL: str
    DYNAMODB_TABLE_NAME: str
    DYNAMODB_ENDPOINT_URL: str | None = None


settings = Settings()
