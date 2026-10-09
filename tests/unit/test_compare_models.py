"""Tests for the model comparison script and its scenarios.

The script's verdicts are only as good as its matching and its expected calls, so both
are tested here: the matching against hand-built calls, and the scenarios against the
bot's real tools, categories and expense list — a typo in an expected argument name
would otherwise fail every model alike and look like a model problem.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from langchain_core.messages import AIMessage
from langchain_core.messages.tool import ToolCall

from scripts import compare_models
from scripts.compare_models import argument_matches, calls_match, run_scenario
from scripts.model_scenarios import (
    EXPENSE_LIST,
    LAST_TUESDAY,
    SCENARIOS,
    TODAY,
    YESTERDAY,
    ExpectedCall,
    Scenario,
)
from src.bot.agent.nodes import tools as BOT_TOOLS
from src.bot.tools.expenses import CATEGORIES

_TOOLS_BY_NAME = {t.name: t for t in BOT_TOOLS}


def _call(name: str, **args: Any) -> ToolCall:
    return ToolCall(name=name, args=args, id=f"{name}-id")


# ── argument and call matching ───────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["8500", "8500.00", 8500, " 8500 "])
def test_numbers_match_by_value(value: object) -> None:
    assert argument_matches("8500", {"amount": value}, "amount")


def test_text_matches_regardless_of_case() -> None:
    assert argument_matches("KRW", {"currency": "krw"}, "currency")


def test_a_different_value_does_not_match() -> None:
    assert not argument_matches("8500", {"amount": "9000"}, "amount")


def test_a_missing_argument_matches_only_when_absence_is_accepted() -> None:
    assert argument_matches(("Card", None), {}, "payment_method")
    assert not argument_matches("Card", {}, "payment_method")
    assert not argument_matches(
        ("Card", None), {"payment_method": "Cash"}, "payment_method"
    )


def test_calls_match_in_any_order() -> None:
    expected = [
        ExpectedCall("edit_expense", {"expense_id": "r3dq"}),
        ExpectedCall("edit_expense", {"expense_id": "w8tn"}),
    ]
    actual = [
        _call("edit_expense", expense_id="w8tn"),
        _call("edit_expense", expense_id="r3dq"),
    ]
    assert calls_match(expected, actual)


def test_an_extra_or_missing_call_fails() -> None:
    expected = [ExpectedCall("get_all_expenses")]
    assert not calls_match(expected, [])
    assert not calls_match(expected, [_call("get_all_expenses"), _call("end_trip")])


def test_no_calls_expected_and_none_made_passes() -> None:
    assert calls_match([], [])


def test_one_call_cannot_satisfy_two_expected_calls() -> None:
    expected = [
        ExpectedCall("add_expense", {"amount": "5500"}),
        ExpectedCall("add_expense", {"amount": "5500"}),
    ]
    assert not calls_match(expected, [_call("add_expense", amount="5500")])


def test_unchecked_arguments_are_ignored() -> None:
    expected = [ExpectedCall("delete_expense", {"expense_id": "r3dq"})]
    actual = [_call("delete_expense", expense_id="r3dq", expected_amount="5500")]
    assert calls_match(expected, actual)


# ── the scenarios themselves ─────────────────────────────────────────────────────────


def test_scenario_names_are_unique() -> None:
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_expected_calls_name_real_tools_and_arguments(scenario: Scenario) -> None:
    for expected in scenario.expected_calls:
        assert expected.tool in _TOOLS_BY_NAME
        assert set(expected.args) <= set(_TOOLS_BY_NAME[expected.tool].args)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_expected_values_are_valid_for_the_bot(scenario: Scenario) -> None:
    for expected in scenario.expected_calls:
        for name, accepted in expected.args.items():
            options = accepted if isinstance(accepted, tuple) else (accepted,)
            for option in options:
                if option is None:
                    continue
                if name == "category":
                    assert option in CATEGORIES
                if name == "expense_id":
                    assert f"| {option} |" in EXPENSE_LIST


def test_edit_and_delete_amounts_are_the_listed_ones() -> None:
    listed = {
        line.split(" | ")[1]: line.split(" | ")[4].split()[0]
        for line in EXPENSE_LIST.splitlines()[1:]
    }
    for scenario in SCENARIOS:
        for expected in scenario.expected_calls:
            if "expected_amount" in expected.args:
                expense_id = expected.args["expense_id"]
                assert isinstance(expense_id, str)
                assert expected.args["expected_amount"] == listed[expense_id]


def test_relative_dates_follow_from_today() -> None:
    today = date.fromisoformat(TODAY)
    assert date.fromisoformat(YESTERDAY) == today - timedelta(days=1)
    tuesday = date.fromisoformat(LAST_TUESDAY)
    assert tuesday.strftime("%A") == "Tuesday"
    assert 0 < (today - tuesday).days < 7


# ── running a scenario ───────────────────────────────────────────────────────────────


def _model_returning(response: AIMessage) -> MagicMock:
    model = MagicMock()
    model.invoke.return_value = response
    return model


def test_a_run_records_the_verdict_tokens_and_calls() -> None:
    scenario = next(s for s in SCENARIOS if s.name == "delete_one")
    response = AIMessage(
        content="",
        tool_calls=[_call("delete_expense", expense_id="r3dq", expected_amount="5500")],
        usage_metadata={
            "input_tokens": 4454,
            "output_tokens": 79,
            "total_tokens": 4533,
            "input_token_details": {"cache_read": 4000, "cache_creation": 0},
        },
        response_metadata={"stopReason": "tool_use"},
    )

    result = run_scenario(_model_returning(response), scenario, run=2)

    assert result.passed
    assert result.run == 2
    assert (result.input_tokens, result.output_tokens) == (4454, 79)
    assert (result.cache_read_tokens, result.cache_write_tokens) == (4000, 0)
    assert result.stop_reason == "tool_use"
    assert result.tool_calls == [
        {
            "name": "delete_expense",
            "args": {"expense_id": "r3dq", "expected_amount": "5500"},
        }
    ]
    assert not result.thought


def test_a_run_sends_the_bot_prompt_before_the_conversation() -> None:
    scenario = next(s for s in SCENARIOS if s.name == "greeting")
    model = _model_returning(AIMessage("Woof!"))

    run_scenario(model, scenario, run=1)

    sent = model.invoke.call_args.args[0]
    assert sent[0].type == "system"
    assert sent[0].content.rstrip().endswith(f"({scenario.timezone}).")
    assert list(sent[1:]) == list(scenario.messages)


def test_a_reasoning_block_is_recorded_as_thought() -> None:
    scenario = next(s for s in SCENARIOS if s.name == "greeting")
    response = AIMessage(
        content=[
            {"type": "reasoning_content", "reasoning_content": {"text": "hmm"}},
            {"type": "text", "text": "Woof!"},
        ]
    )

    result = run_scenario(_model_returning(response), scenario, run=1)

    assert result.thought
    assert result.passed


def test_a_wrong_call_fails_the_scenario() -> None:
    scenario = next(s for s in SCENARIOS if s.name == "edit_amount")
    response = AIMessage(
        content="",
        tool_calls=[
            _call(
                "edit_expense",
                expense_id="k7m2",
                expected_amount="12000",
                amount="9000",
            )
        ],
    )

    assert not run_scenario(_model_returning(response), scenario, run=1).passed


# ── the command line ─────────────────────────────────────────────────────────────────


def test_a_rejected_request_stops_the_run_with_exit_code_1() -> None:
    model = MagicMock()
    model.invoke.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not available"}},
        "Converse",
    )
    with (
        patch.object(compare_models, "build_model", return_value=model),
        patch.object(compare_models, "resolve_profile", return_value=None),
        patch("boto3.Session"),
    ):
        assert compare_models.main(["--model", "m"]) == 1
    model.invoke.assert_called_once()


def test_only_runs_the_named_scenarios(tmp_path: Any) -> None:
    model = _model_returning(AIMessage("Woof!"))
    out = tmp_path / "results.json"
    with (
        patch.object(compare_models, "build_model", return_value=model) as build,
        patch.object(compare_models, "resolve_profile", return_value=None),
        patch("boto3.Session"),
    ):
        code = compare_models.main(
            [
                "--model",
                "m",
                "--no-temperature",
                "--only",
                "greeting",
                "--json",
                str(out),
            ]
        )

    assert code == 0
    assert model.invoke.call_count == 1
    assert build.call_args.args[2] is None
    assert '"scenario": "greeting"' in out.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "argv",
    [
        ["--model", "m", "--runs", "0"],
        ["--model", "m", "--request-fields", "[1]"],
        ["--model", "m", "--request-fields", "{not json"],
        ["--model", "m", "--temperature", "0.5", "--no-temperature"],
        [],
    ],
)
def test_invalid_arguments_are_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        compare_models.main(argv)
    assert exit_info.value.code == 2
