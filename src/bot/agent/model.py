"""Construct the tool-bound Bedrock model shared by the bot and evaluations."""

from typing import Any, Protocol

from botocore.client import BaseClient
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import BaseMessage

from src.bot.tools import expenses, trip

tools = [
    expenses.add_expense,
    expenses.edit_expense,
    expenses.delete_expense,
    expenses.get_all_expenses,
    trip.start_trip,
    trip.set_trip_timezone,
    trip.end_trip,
]


class ChatModel(Protocol):
    """The tool-bound model interface used by the graph and evaluator."""

    def invoke(self, messages: list[BaseMessage], /) -> BaseMessage:
        """Return the next assistant message, or raise the provider's error."""
        ...


def build_bedrock_model(
    client: BaseClient,
    model_id: str,
    *,
    temperature: float | None,
    max_tokens: int | None = None,
    request_fields: dict[str, Any] | None = None,
) -> ChatModel:
    """Bind the bot's tools to a Bedrock Converse model.

    Args:
        client: Bedrock Runtime client for the chosen model's AWS Region.
        model_id: Foundation model or inference profile ID.
        temperature: Sampling temperature, or None to omit it.
        max_tokens: Optional output-token limit.
        request_fields: Optional model-specific Converse request fields.

    Returns:
        A model that accepts LangChain messages and returns a message or tool calls.

    Raises:
        ValueError: If the model configuration is invalid.
    """
    return ChatBedrockConverse(
        client=client,
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
        additional_model_request_fields=request_fields,
    ).bind_tools(tools)


def build_analysis_model(
    client: BaseClient,
    model_id: str,
    *,
    temperature: float | None,
    max_tokens: int,
) -> ChatModel:
    """Build the model that writes the post-trip insights, with no tools bound.

    Separate from build_bedrock_model, which always binds the bot's tools: the insights
    call only writes text, and a model with no tools cannot act on anything it is told.

    Args:
        client: Bedrock Runtime client for the chosen model's AWS Region.
        model_id: Foundation model or inference profile ID.
        temperature: Sampling temperature, or None to omit it.
        max_tokens: Output-token limit.

    Returns:
        A model that accepts LangChain messages and returns a text message.

    Raises:
        ValueError: If the model configuration is invalid.
    """
    return ChatBedrockConverse(
        client=client,
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
    )
