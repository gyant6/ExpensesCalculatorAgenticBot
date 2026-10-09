"""Run the fixed scenarios against a Bedrock model and report correctness, speed and tokens.

Run from the repository root, with the same .env the dev runner uses:

    uv run python -m scripts.compare_models --model <bedrock_model_id from terraform.tfvars>
    uv run python -m scripts.compare_models --model global.anthropic.claude-haiku-5-5 \\
        --no-temperature --request-fields '{"...": "..."}' --runs 3
    uv run python -m scripts.compare_models --model <id> --only edit_amount delete_one

Each scenario is one call with the bot's real system prompt and tool definitions (see
scripts/model_scenarios.py). The response's tool calls are checked against the
scenario's expected calls; the tools themselves never run, so nothing touches DynamoDB.
The temperature defaults to the bot's MODEL_TEMPERATURE, so a run with only --model set
to production's model is the baseline the alternatives are compared against.

--request-fields is passed to Converse as additionalModelRequestFields unchanged, so
model-specific settings such as effort or thinking can be tried without changing code.

The AWS profile is resolved as for scripts/deploy_lambda.py: --profile, AWS_PROFILE,
`aws_profile` in terraform/local.auto.tfvars, then the default credential chain.

Exit codes:
    0: every call completed; failed scenarios are reported, not treated as errors.
    1: Bedrock rejected a request — a model, quota, permission or request-field problem
       that would fail every scenario the same way, so the run stops at the first.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from itertools import permutations
from pathlib import Path
from typing import Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.runnables import Runnable

from scripts.deploy_lambda import resolve_profile
from scripts.model_scenarios import SCENARIOS, Accepted, ExpectedCall, Scenario
from src.bot.agent.nodes import tools as BOT_TOOLS
from src.bot.agent.prompts import get_system_prompt
from src.bot.config import settings

# A slow call is data to record, not a reason to give up; the 30 s gateway limit is what
# the bot has to fit within, so allow well past it before treating a call as hung.
_CLIENT_CONFIG: Final = Config(
    connect_timeout=10,
    read_timeout=120,
    retries={"mode": "standard", "max_attempts": 3},
)

# Content block types langchain-aws uses for the model's reasoning, across versions.
_REASONING_BLOCK_TYPES: Final = frozenset(
    {"reasoning_content", "reasoning", "thinking"}
)

_REPLY_PREVIEW_CHARS: Final = 60

ChatModel = Runnable[LanguageModelInput, BaseMessage]


@dataclass(frozen=True)
class CallResult:
    """The outcome of one scenario run.

    Attributes:
        scenario: The scenario's name.
        run: Which repetition this was, from 1.
        passed: Whether the response made exactly the expected tool calls.
        seconds: Wall-clock time of the call.
        input_tokens: Input tokens Bedrock reported, cache reads and writes included.
        output_tokens: Output tokens, thinking included.
        cache_read_tokens: Input tokens served from the prompt cache.
        cache_write_tokens: Input tokens written to the prompt cache.
        thought: Whether the response contained a reasoning block.
        stop_reason: Converse's stopReason, e.g. "tool_use" or "end_turn".
        tool_calls: The calls the model made, as name and arguments.
        reply: The start of the response's text, for scenarios answered in words.
    """

    scenario: str
    run: int
    passed: bool
    seconds: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    thought: bool
    stop_reason: str | None
    tool_calls: list[dict[str, Any]]
    reply: str


def _normalise(value: object) -> Decimal | str:
    """Comparable form of an argument: numbers by value, text case-insensitively."""
    text = str(value).strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text.casefold()
    return number if number.is_finite() else text.casefold()


def argument_matches(accepted: Accepted, args: Mapping[str, object], name: str) -> bool:
    """Whether a call's argument is one of the accepted values.

    Args:
        accepted: One accepted value, or a tuple of them; None in a tuple accepts the
            argument being left out.
        args: The arguments the model passed.
        name: The argument to check.

    Returns:
        True if the argument is absent and absence is accepted, or present and equal
        to an accepted value — numerically for numbers ("8500" equals "8500.00"),
        case-insensitively for text.
    """
    options = accepted if isinstance(accepted, tuple) else (accepted,)
    value = args.get(name)
    if value is None:
        return None in options
    actual = _normalise(value)
    return any(
        option is not None and _normalise(option) == actual for option in options
    )


def call_matches(expected: ExpectedCall, actual: ToolCall) -> bool:
    """Whether a tool call is the expected tool with every checked argument accepted."""
    return actual["name"] == expected.tool and all(
        argument_matches(accepted, actual["args"], name)
        for name, accepted in expected.args.items()
    )


def calls_match(expected: Sequence[ExpectedCall], actual: Sequence[ToolCall]) -> bool:
    """Whether the actual calls are exactly the expected ones, in any order.

    Every expected call must be matched by a different actual call, and no actual call
    may be left over. The orderings are tried exhaustively — scenarios expect at most a
    few calls — so two similar calls cannot be paired the wrong way round.
    """
    if len(expected) != len(actual):
        return False
    return any(
        all(call_matches(e, a) for e, a in zip(expected, ordering, strict=True))
        for ordering in permutations(actual)
    )


def _thought(response: AIMessage) -> bool:
    """Whether the response contains a reasoning block."""
    if not isinstance(response.content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") in _REASONING_BLOCK_TYPES
        for block in response.content
    )


def run_scenario(model: ChatModel, scenario: Scenario, run: int) -> CallResult:
    """Send one scenario to the model and check its response.

    Args:
        model: The chat model with the bot's tools bound.
        scenario: The conversation and expected calls.
        run: The repetition number, recorded in the result.

    Returns:
        The response's correctness, timing, token counts and calls.

    Raises:
        botocore.exceptions.ClientError: If Bedrock rejects the request.
        TypeError: If the model returns something other than an AIMessage.
    """
    system = SystemMessage(
        get_system_prompt(
            scenario.trip_start_date, scenario.local_date, scenario.timezone
        )
    )
    started = time.perf_counter()
    response = model.invoke([system, *scenario.messages])
    seconds = time.perf_counter() - started
    if not isinstance(response, AIMessage):
        raise TypeError(f"Expected an AIMessage, got {type(response).__name__}")

    usage = response.usage_metadata
    details = (usage.get("input_token_details") if usage else None) or {}
    return CallResult(
        scenario=scenario.name,
        run=run,
        passed=calls_match(scenario.expected_calls, response.tool_calls),
        seconds=round(seconds, 2),
        input_tokens=usage["input_tokens"] if usage else 0,
        output_tokens=usage["output_tokens"] if usage else 0,
        cache_read_tokens=details.get("cache_read", 0),
        cache_write_tokens=details.get("cache_creation", 0),
        thought=_thought(response),
        stop_reason=response.response_metadata.get("stopReason"),
        tool_calls=[
            {"name": c["name"], "args": c["args"]} for c in response.tool_calls
        ],
        reply=response.text[:_REPLY_PREVIEW_CHARS].replace("\n", " "),
    )


def _describe_calls(result: CallResult) -> str:
    """The calls a response made, or the start of its text when it made none."""
    if not result.tool_calls:
        return f'text: "{result.reply}"'
    return "; ".join(
        f"{c['name']}({', '.join(f'{k}={v}' for k, v in c['args'].items())})"
        for c in result.tool_calls
    )


def print_report(results: Sequence[CallResult]) -> None:
    """Print one line per call, then totals across the run."""
    print(
        f"{'scenario':<20} {'run':>3} {'ok':<4} {'secs':>6} {'in':>6} {'out':>5} "
        f"{'cache r/w':>11} {'think':<5} calls"
    )
    for r in results:
        print(
            f"{r.scenario:<20} {r.run:>3} {'PASS' if r.passed else 'FAIL':<4} "
            f"{r.seconds:>6.2f} {r.input_tokens:>6} {r.output_tokens:>5} "
            f"{f'{r.cache_read_tokens}/{r.cache_write_tokens}':>11} "
            f"{'yes' if r.thought else 'no':<5} {_describe_calls(r)}"
        )

    seconds = [r.seconds for r in results]
    print()
    print(f"passed        {sum(r.passed for r in results)}/{len(results)}")
    print(
        f"seconds       median {statistics.median(seconds):.2f}, max {max(seconds):.2f}"
    )
    print(
        f"tokens        {sum(r.input_tokens for r in results)} in, "
        f"{sum(r.output_tokens for r in results)} out, "
        f"{sum(r.cache_read_tokens for r in results)} read from cache"
    )
    print(f"thought in    {sum(r.thought for r in results)}/{len(results)} calls")


def build_model(
    session: boto3.Session,
    model_id: str,
    temperature: float | None,
    request_fields: dict[str, Any] | None,
) -> ChatModel:
    """The chat model with the bot's tools bound, as agent_node calls it."""
    llm = ChatBedrockConverse(
        client=session.client(
            "bedrock-runtime", region_name=settings.AWS_REGION, config=_CLIENT_CONFIG
        ),
        model_id=model_id,
        temperature=temperature,
        additional_model_request_fields=request_fields,
    )
    return llm.bind_tools(BOT_TOOLS)


def _parse_request_fields(raw: str) -> dict[str, Any]:
    """Parse --request-fields, rejecting anything but a JSON object."""
    try:
        fields = json.loads(raw)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(f"not valid JSON: {error}") from error
    if not isinstance(fields, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return fields


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the selected scenarios and print the report.

    Args:
        argv: Command-line arguments without the program name; sys.argv when None.

    Returns:
        The process exit code, as described in the module docstring.
    """
    names = [s.name for s in SCENARIOS]
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    # Required rather than defaulted from settings: the local .env is not necessarily
    # the model production runs, which is bedrock_model_id in terraform.tfvars.
    parser.add_argument(
        "--model",
        required=True,
        help="Bedrock model or inference profile ID",
    )
    temperature = parser.add_mutually_exclusive_group()
    temperature.add_argument(
        "--temperature",
        type=float,
        default=settings.MODEL_TEMPERATURE,
        help="sampling temperature (default: the bot's MODEL_TEMPERATURE)",
    )
    temperature.add_argument(
        "--no-temperature",
        action="store_true",
        help="send no temperature, for models that reject sampling parameters",
    )
    parser.add_argument(
        "--request-fields",
        type=_parse_request_fields,
        help="JSON object passed to Converse as additionalModelRequestFields",
    )
    parser.add_argument(
        "--runs", type=int, default=1, help="repetitions of each scenario (default: 1)"
    )
    parser.add_argument(
        "--only", nargs="+", choices=names, metavar="SCENARIO", help="run only these"
    )
    parser.add_argument(
        "--profile", help="AWS profile (default: resolved as for deploys)"
    )
    parser.add_argument(
        "--json", type=Path, help="also write every result to this file"
    )
    args = parser.parse_args(argv)
    # Replies carry emoji, which a Windows console's legacy code page cannot encode.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    temperature_sent = None if args.no_temperature else args.temperature
    model = build_model(
        boto3.Session(profile_name=resolve_profile(args.profile)),
        args.model,
        temperature_sent,
        args.request_fields,
    )
    selected = [s for s in SCENARIOS if not args.only or s.name in args.only]
    print(
        f"model {args.model}, temperature {temperature_sent}, "
        f"request fields {json.dumps(args.request_fields)}, "
        f"{len(selected)} scenarios x {args.runs}\n"
    )

    results: list[CallResult] = []
    try:
        for run in range(1, args.runs + 1):
            for scenario in selected:
                results.append(run_scenario(model, scenario, run))
    except (ClientError, BotoCoreError) as error:
        print(f"Bedrock rejected the request: {error}", file=sys.stderr)
        return 1

    # Written before printing, so a run that has been paid for is never lost to a
    # display problem.
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "model": args.model,
                    "temperature": temperature_sent,
                    "request_fields": args.request_fields,
                    "results": [asdict(r) for r in results],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"results written to {args.json}\n")
    print_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
