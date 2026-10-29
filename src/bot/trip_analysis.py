"""A short insights message about a finished trip, written by a separate model.

The trip's totals are sent first, calculated in code. This step then asks a model to pick
a few things worth saying about how the money was spent. It is given only the figures
trip_facts computes, and its reply is checked before it is sent: a figure the facts do not
contain, markdown, or an over-long reply is dropped, and the user simply does not get the
extra message. Everything here is best effort and never raises into the caller, because it
runs after the trip has already ended.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import boto3.session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.bot.agent.model import ChatModel, build_analysis_model
from src.bot.config import settings
from src.bot.trip_analysis_prompt import HUMAN_TEMPLATE, SYSTEM_PROMPT
from src.bot.trip_facts import TripFacts, compute_trip_facts

logger = logging.getLogger(__name__)

# Longest insights message sent. The prompt asks for about six short lines; this is the
# backstop for a reply that ignores it.
MAX_INSIGHTS_CHARS: Final = 1_200

# Time to establish a connection. The read timeout is ANALYSIS_TIMEOUT_SECONDS.
_CONNECT_TIMEOUT_SECONDS: Final = 3

_NUMBER: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Small whole numbers a reply may use freely, as in "the top 3" or "two of the five".
_FREE_INTEGERS: Final = frozenset(Decimal(n) for n in range(1, 6))
# Comparisons such as "3x", "2.5 times" or "twice" are arithmetic the model was told not
# to do, and are not in the facts to check against.
_MULTIPLIER: Final = re.compile(
    r"\d\s*(?:x|×)(?!\w)|\d\s*times\b|\b(?:twice|thrice)\b", re.IGNORECASE
)
# Markdown the chat would show literally, as the bot sends plain text (ADR 0011).
_MARKDOWN: Final = re.compile(r"\*\*|__|`|^\s*(?:[-*#>•]|\d+\.)\s", re.MULTILINE)

_model: ChatModel | None = None
_model_lock = threading.Lock()


@dataclass(frozen=True)
class AnalysisResult:
    """What the insights call produced.

    Attributes:
        text: The message to send, or None when nothing should be sent.
        input_tokens: Input tokens the call used, 0 if it did not complete.
        output_tokens: Output tokens the call used, 0 if it did not complete.
        problem: Why text is None, for the log; None when text was produced.
        rejected_reply: The reply a check dropped, for tuning the prompt; None otherwise.
    """

    text: str | None
    input_tokens: int
    output_tokens: int
    problem: str | None
    rejected_reply: str | None = None


def _numbers_in(text: str) -> set[Decimal]:
    """Every number in the text, as a value, so 500 and 500.00 are the same figure."""
    return {Decimal(token.replace(",", "")) for token in _NUMBER.findall(text)}


def find_problem(reply: str, facts_text: str) -> str | None:
    """Check a model reply before it is sent.

    Args:
        reply: The model's reply.
        facts_text: The fact sheet the model was given; every figure in the reply must be
            one of its numbers.

    Returns:
        A description of the first problem found, or None if the reply may be sent.
    """
    if not reply.strip():
        return "empty reply"
    if len(reply) > MAX_INSIGHTS_CHARS:
        return f"{len(reply)} characters exceeds the {MAX_INSIGHTS_CHARS} limit"
    if _MARKDOWN.search(reply):
        return "markdown formatting"
    if _MULTIPLIER.search(reply):
        return "multiplier comparison"
    unknown = sorted(_numbers_in(reply) - _numbers_in(facts_text) - _FREE_INTEGERS)
    if unknown:
        return f"figures not in the facts: {[format(n, 'f') for n in unknown]}"
    return None


def write_insights(facts: TripFacts, model: ChatModel) -> AnalysisResult:
    """Ask the model for an insights message about the trip, and check what it says.

    Args:
        facts: The trip's computed figures.
        model: The insights model.

    Returns:
        The result; its text is None when the call failed or the reply failed a check.
        Never raises for a model or provider failure.
    """
    facts_text = facts.to_prompt_text()
    messages = [
        SystemMessage(SYSTEM_PROMPT),
        HumanMessage(HUMAN_TEMPLATE.format(facts=facts_text)),
    ]
    try:
        response = model.invoke(messages)
    except (ClientError, BotoCoreError, ValueError) as error:
        logger.warning("Trip insights call failed: %s", error, exc_info=True)
        return AnalysisResult(None, 0, 0, f"model call failed: {type(error).__name__}")

    input_tokens = output_tokens = 0
    if isinstance(response, AIMessage) and response.usage_metadata is not None:
        input_tokens = response.usage_metadata["input_tokens"]
        output_tokens = response.usage_metadata["output_tokens"]

    reply = response.text.strip()
    problem = find_problem(reply, facts_text)
    if problem is not None:
        logger.warning("Trip insights rejected: %s. Reply was: %r", problem, reply)
        return AnalysisResult(None, input_tokens, output_tokens, problem, reply)
    return AnalysisResult(reply, input_tokens, output_tokens, None)


def _get_model(model_id: str) -> ChatModel:
    """Return the insights model, building it and its client on first use.

    The client has no retries and a short read timeout: a slow model is abandoned, since
    the user already has everything that matters. It is built from a session of its own,
    as boto3's default session is not safe to share between threads.

    Args:
        model_id: Foundation model or inference profile ID.

    Returns:
        The shared insights model.

    Raises:
        botocore.exceptions.BotoCoreError: If the client cannot be created.
        ValueError: If the model configuration is invalid.
    """
    global _model
    with _model_lock:
        if _model is None:
            client = boto3.session.Session().client(
                "bedrock-runtime",
                region_name=settings.AWS_BEDROCK_REGION or settings.AWS_REGION,
                config=Config(
                    connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=settings.ANALYSIS_TIMEOUT_SECONDS,
                    retries={"total_max_attempts": 1},
                ),
            )
            _model = build_analysis_model(
                client,
                model_id,
                temperature=settings.ANALYSIS_TEMPERATURE,
                max_tokens=settings.ANALYSIS_MAX_OUTPUT_TOKENS,
            )
        return _model


def reset_model() -> None:
    """Discard the shared insights model so the next call builds a new one (for tests)."""
    global _model
    with _model_lock:
        _model = None


def analyse_trip(csv_bytes: bytes | None) -> AnalysisResult | None:
    """Write the insights message for a trip that has just ended.

    Args:
        csv_bytes: The CSV delivered to the user, or None if there is none.

    Returns:
        None when no message is attempted: insights are turned off, there is no CSV, the
        CSV is damaged, or the trip has no expenses or an unconverted one. Otherwise the
        result, whose text is None if nothing should be sent. Never raises for a model,
        provider or CSV problem, so the caller can run it after the trip has ended.
    """
    model_id = settings.AWS_BEDROCK_ANALYSIS_MODEL_ID
    if model_id is None or csv_bytes is None:
        return None
    try:
        facts = compute_trip_facts(csv_bytes)
    except ValueError:
        logger.warning(
            "Trip insights skipped: the CSV could not be read", exc_info=True
        )
        return None
    if facts is None:
        return None
    try:
        model = _get_model(model_id)
    except (BotoCoreError, ValueError) as error:
        logger.warning("Trip insights model unavailable: %s", error, exc_info=True)
        return AnalysisResult(None, 0, 0, f"model unavailable: {type(error).__name__}")
    return write_insights(facts, model)
