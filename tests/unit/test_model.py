"""Check the shared Bedrock construction used by production and evaluation."""

from unittest.mock import MagicMock, patch

import boto3
import pytest

from scripts.compare_models import build_model
from src.bot.agent import nodes
from src.bot.agent.model import build_bedrock_model, tools
from src.bot.config import settings


def test_shared_factory_binds_the_real_tools_and_request_settings() -> None:
    """Both callers must receive the same tool schema and request parameters."""
    client = MagicMock()
    with patch("src.bot.agent.model.ChatBedrockConverse") as constructor:
        bound = build_bedrock_model(
            client,
            "minimax.minimax-m2.5",
            temperature=None,
            max_tokens=4096,
            request_fields={"example": True},
        )

    assert bound is constructor.return_value.bind_tools.return_value
    constructor.assert_called_once_with(
        client=client,
        model_id="minimax.minimax-m2.5",
        temperature=None,
        max_tokens=4096,
        additional_model_request_fields={"example": True},
    )
    constructor.return_value.bind_tools.assert_called_once_with(tools)


def test_production_model_uses_bedrock_region_without_moving_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Sydney Bedrock client must not alter the application's AWS_REGION."""
    client = MagicMock()
    model = MagicMock()
    app_region = settings.AWS_REGION
    monkeypatch.setattr(nodes, "llm_with_tools", None)
    monkeypatch.setattr(settings, "AWS_BEDROCK_REGION", "ap-southeast-2")
    monkeypatch.setattr(settings, "AWS_BEDROCK_MODEL_ID", "minimax.minimax-m2.5")
    monkeypatch.setattr(settings, "MODEL_TEMPERATURE", None)
    monkeypatch.setattr(settings, "MODEL_MAX_OUTPUT_TOKENS", 4096)
    with (
        patch.object(boto3, "client", return_value=client) as boto_client,
        patch.object(nodes, "build_bedrock_model", return_value=model) as factory,
    ):
        assert nodes._default_model() is model

    boto_client.assert_called_once_with("bedrock-runtime", region_name="ap-southeast-2")
    factory.assert_called_once_with(
        client, "minimax.minimax-m2.5", temperature=None, max_tokens=4096
    )
    assert settings.AWS_REGION == app_region


def test_evaluator_uses_the_shared_factory() -> None:
    """Evaluation must exercise the model settings production passes to Converse."""
    session = MagicMock()
    model = MagicMock()
    with patch(
        "scripts.compare_models.build_bedrock_model", return_value=model
    ) as factory:
        assert (
            build_model(
                session,
                "minimax.minimax-m2.5",
                None,
                None,
                region="ap-southeast-2",
                max_tokens=4096,
            )
            is model
        )

    assert session.client.call_args.kwargs["region_name"] == "ap-southeast-2"
    factory.assert_called_once_with(
        session.client.return_value,
        "minimax.minimax-m2.5",
        temperature=None,
        request_fields=None,
        max_tokens=4096,
    )
