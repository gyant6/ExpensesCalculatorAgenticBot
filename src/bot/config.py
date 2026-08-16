"""Global application settings loaded via pydantic-settings.

Local development: reads from a `.env` file.
Production (Lambda): non-sensitive values come from Lambda environment variables;
    sensitive values (TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_ID) are fetched from
    AWS SSM Parameter Store at startup. The Lambda environment variables
    TELEGRAM_BOT_TOKEN_SSM_PATH and ADMIN_TELEGRAM_ID_SSM_PATH hold the SSM
    parameter names — the actual secrets never appear in Lambda configuration.
"""

import logging
import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# SSM fields: env var that holds the path → env var that receives the value.
_SSM_FIELDS: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN_SSM_PATH": "TELEGRAM_BOT_TOKEN",
    "ADMIN_TELEGRAM_ID_SSM_PATH": "ADMIN_TELEGRAM_ID",
}


def _load_ssm_secrets() -> None:
    """Fetch sensitive settings from SSM and inject them into os.environ.

    Only runs when ENVIRONMENT=production. Reads SSM parameter paths from
    dedicated *_SSM_PATH environment variables, fetches their values in a single
    batch call, and writes the results back into os.environ so pydantic-settings
    can read them in the normal way.

    Raises:
        RuntimeError: If any expected SSM parameter path env var is set but the
            corresponding parameter is missing from SSM (indicates a deployment
            misconfiguration rather than a transient error).
        botocore.exceptions.ClientError: If the SSM API call fails (IAM
            permission denied, network error, etc.).
    """
    if os.environ.get("ENVIRONMENT") != "production":
        return

    import boto3

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    ssm = boto3.client("ssm", region_name=region)

    paths: dict[str, str] = {}
    for path_env, value_env in _SSM_FIELDS.items():
        path = os.environ.get(path_env)
        if path:
            paths[path] = value_env

    if not paths:
        return

    response = ssm.get_parameters(Names=list(paths.keys()), WithDecryption=True)
    found = {p["Name"]: p["Value"] for p in response["Parameters"]}

    missing = [name for name in paths if name not in found]
    if missing:
        raise RuntimeError(
            f"SSM parameters not found: {missing}. Verify they exist and the "
            "Lambda execution role has ssm:GetParameters permission."
        )

    for ssm_path, value_env in paths.items():
        os.environ[value_env] = found[ssm_path]


_load_ssm_secrets()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    ENVIRONMENT: str
    TELEGRAM_BOT_TOKEN: str
    AWS_BEDROCK_MODEL_ID: str
    AWS_REGION: str
    LOG_LEVEL: str
    DYNAMODB_TABLE_NAME: str
    DYNAMODB_ENDPOINT_URL: str | None = None
    ADMIN_TELEGRAM_ID: int
    # Abandoned conversation threads are expired after this many seconds. The DynamoDBSaver
    # writes a `ttl` epoch attribute on every checkpoint; DynamoDB's TTL process deletes
    # items past that timestamp automatically. Defaults to 90 days.
    CHECKPOINT_TTL_SECONDS: int = 7_776_000

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
