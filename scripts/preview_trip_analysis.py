"""Preview the post-trip insights message on sample trips, to tune its prompt.

Without --model it only prints each trip's fact sheet, the text the model would be given,
and makes no request. With --model it asks that Bedrock model for the insights message
the same way the bot does and reports whether the bot's checks would have sent it. Those
requests are paid.

    uv run python -m scripts.preview_trip_analysis scripts/trip_fixtures/*.csv
    uv run python -m scripts.preview_trip_analysis scripts/trip_fixtures/two_weeks.csv \\
        --model global.anthropic.claude-sonnet-4-6 --runs 3

The AWS profile is taken, first match wins, from --profile, the AWS_PROFILE variable, or
`aws_profile` in the gitignored terraform/local.auto.tfvars, as for the deploy script.
The exit status is 1 if any reply would have been dropped, so the sample trips double as
a check on the prompt.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DEFAULT_RUNS = 1
CONNECT_TIMEOUT_SECONDS = 5


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("csv", nargs="+", type=Path, help="trip CSV(s) to preview")
    parser.add_argument("--model", help="Bedrock model or inference profile ID")
    parser.add_argument("--region", help="Bedrock region; defaults to the bot's")
    parser.add_argument("--profile", help="AWS profile; see the module docstring")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="calls per trip")
    parser.add_argument(
        "--max-tokens", type=int, help="output limit; defaults to the bot's"
    )
    parser.add_argument("--temperature", type=float, help="omitted by default")
    parser.add_argument(
        "--timeout", type=int, help="read timeout in seconds; defaults to the bot's"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Print each trip's fact sheet and, with --model, the insights the model writes.

    Args:
        argv: Command-line arguments, or None to use sys.argv.

    Returns:
        0 if every reply would have been sent (or no model was requested), 1 if any
        reply would have been dropped or any call failed.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    # Imported here so a bad option fails before configuration, which needs the bot's
    # environment, is loaded.
    import boto3
    from botocore.config import Config

    from scripts.deploy_lambda import resolve_profile
    from src.bot.agent.model import build_analysis_model
    from src.bot.config import settings
    from src.bot.trip_analysis import write_insights
    from src.bot.trip_facts import compute_trip_facts

    model = None
    if args.model:
        client = boto3.Session(profile_name=resolve_profile(args.profile)).client(
            "bedrock-runtime",
            region_name=args.region
            or settings.AWS_BEDROCK_REGION
            or settings.AWS_REGION,
            config=Config(
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=args.timeout or settings.ANALYSIS_TIMEOUT_SECONDS,
                retries={"total_max_attempts": 1},
            ),
        )
        model = build_analysis_model(
            client,
            args.model,
            temperature=args.temperature,
            max_tokens=args.max_tokens or settings.ANALYSIS_MAX_OUTPUT_TOKENS,
        )

    dropped = 0
    for path in args.csv:
        print(f"\n===== {path.name} =====")
        facts = compute_trip_facts(path.read_bytes())
        if facts is None:
            print("No analysis: the trip has no expenses or an unconverted one.")
            continue
        print(facts.to_prompt_text())
        if model is None:
            continue
        for run in range(1, args.runs + 1):
            started = time.perf_counter()
            result = write_insights(facts, model)
            seconds = time.perf_counter() - started
            verdict = "WOULD SEND" if result.text else f"DROPPED: {result.problem}"
            print(
                f"\n--- run {run}: {verdict} "
                f"({result.input_tokens} in, {result.output_tokens} out, {seconds:.1f}s)"
            )
            if result.text:
                print(result.text)
            else:
                dropped += 1
                if result.rejected_reply:
                    print(result.rejected_reply)
    return 1 if dropped else 0


if __name__ == "__main__":
    sys.exit(main())
