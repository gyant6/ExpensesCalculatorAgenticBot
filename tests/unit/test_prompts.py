"""Tests for the system prompt builder."""

from __future__ import annotations

from src.bot.agent.prompts import get_system_prompt


def test_no_active_trip_mentions_no_active_trip() -> None:
    prompt = get_system_prompt(trip_start_date=None)
    assert "no active trip" in prompt


def test_no_active_trip_does_not_include_start_date() -> None:
    prompt = get_system_prompt(trip_start_date=None)
    assert "began recording on" not in prompt


def test_active_trip_includes_start_date() -> None:
    prompt = get_system_prompt(trip_start_date="2026-08-01")
    assert "2026-08-01" in prompt


def test_active_trip_does_not_mention_no_active_trip() -> None:
    prompt = get_system_prompt(trip_start_date="2026-08-01")
    assert "no active trip" not in prompt


def test_prompt_always_includes_tools_list() -> None:
    for date in (None, "2026-08-01"):
        prompt = get_system_prompt(trip_start_date=date)
        assert "start_trip" in prompt
        assert "end_trip" in prompt
        assert "add_expense" in prompt


def test_prompt_always_includes_sgd_dollar_sign_instruction() -> None:
    for date in (None, "2026-08-01"):
        prompt = get_system_prompt(trip_start_date=date)
        assert 'The "$" symbol means SGD' in prompt
