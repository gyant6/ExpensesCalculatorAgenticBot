"""Tests for the system prompt builder."""

from __future__ import annotations

from src.bot.agent.prompts import get_system_prompt

_TODAY = "2026-09-14"
_ZONE = "America/Los_Angeles"


def _prompt(trip_start_date: str | None) -> str:
    return get_system_prompt(trip_start_date, _TODAY, _ZONE)


def test_no_active_trip_mentions_no_active_trip() -> None:
    assert "no active trip" in _prompt(None)


def test_no_active_trip_does_not_include_start_date() -> None:
    assert "began recording on" not in _prompt(None)


def test_active_trip_includes_start_date() -> None:
    assert "2026-08-01" in _prompt("2026-08-01")


def test_active_trip_does_not_mention_no_active_trip() -> None:
    assert "no active trip" not in _prompt("2026-08-01")


def test_prompt_always_includes_tools_list() -> None:
    for date in (None, "2026-08-01"):
        prompt = _prompt(date)
        assert "start_trip" in prompt
        assert "set_trip_timezone" in prompt
        assert "end_trip" in prompt
        assert "add_expense" in prompt


def test_prompt_ends_with_today_in_the_trip_zone() -> None:
    # The model is given the local date with its weekday, so it never converts zones and
    # can resolve "yesterday" or "on Tuesday" itself. It answered "today" with "I don't
    # have today's date" when the prompt carried none.
    for date in (None, "2026-08-01"):
        assert (
            _prompt(date)
            .rstrip()
            .endswith("Today is Monday, 14 September 2026 (America/Los_Angeles).")
        )


def test_only_the_date_line_changes_from_day_to_day() -> None:
    # Everything before the date line must stay byte-identical across days, or caching
    # the stable prefix would miss every morning.
    monday = get_system_prompt("2026-08-01", "2026-09-14", _ZONE)
    tuesday = get_system_prompt("2026-08-01", "2026-09-15", _ZONE)
    prefix = monday.rsplit("Today is", 1)[0]
    assert tuesday.startswith(prefix)
    assert monday != tuesday


def test_prompt_never_asks_for_the_date_and_defaults_to_card() -> None:
    for date in (None, "2026-08-01"):
        prompt = _prompt(date)
        assert "Never ask for the date" in prompt
        assert "use Card and do not ask" in prompt
        assert "Always infer the category" in prompt


def test_prompt_keeps_expense_ids_away_from_the_user() -> None:
    for date in (None, "2026-08-01"):
        assert "Never show expense ids to the user" in _prompt(date)


def test_prompt_forbids_re_adding_expenses_from_memory() -> None:
    # On a real trip the model "restored" a list from memory, double-counting three
    # expenses and inventing a fourth.
    for date in (None, "2026-08-01"):
        assert "Never re-add expenses you believe are missing" in _prompt(date)


def test_prompt_always_includes_sgd_dollar_sign_instruction() -> None:
    for date in (None, "2026-08-01"):
        assert 'The "$" symbol means SGD' in _prompt(date)
