"""Tests for webhook delivery authentication.

The gateway URL is public, so this header is the only thing distinguishing a genuine
Telegram delivery from a forged update naming the admin's Telegram ID — which would
otherwise pass the auth gate and reach the /auth commands.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.bot.config import settings
from src.bot.main import _SECRET_TOKEN_HEADER, _is_authentic, lambda_handler

_SECRET = "s3cret-webhook-token"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WEBHOOK_SECRET", _SECRET)


def _event(token: str | None = _SECRET, **extra: Any) -> dict[str, Any]:
    headers = {} if token is None else {_SECRET_TOKEN_HEADER: token}
    return {"headers": headers, "body": '{"update_id": 1}', **extra}


@pytest.mark.usefixtures("configured")
def test_accepts_a_delivery_carrying_the_secret() -> None:
    assert _is_authentic(_event()) is True


@pytest.mark.usefixtures("configured")
def test_rejects_a_wrong_secret() -> None:
    assert _is_authentic(_event(token="not-the-secret")) is False


@pytest.mark.usefixtures("configured")
def test_rejects_a_missing_header() -> None:
    assert _is_authentic(_event(token=None)) is False


@pytest.mark.usefixtures("configured")
def test_rejects_an_event_with_no_headers_at_all() -> None:
    assert _is_authentic({"body": "{}"}) is False


def test_fails_closed_when_no_secret_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A misdeploy that leaves the secret unset must reject everything rather than accept
    # everything — the opposite default would silently expose the bot.
    monkeypatch.setattr(settings, "WEBHOOK_SECRET", None)

    assert _is_authentic(_event()) is False


def test_handler_returns_403_and_never_processes_an_unauthenticated_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WEBHOOK_SECRET", _SECRET)

    def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the update reached the graph despite a bad secret")

    monkeypatch.setattr("src.bot.main._handle_update", _fail)

    assert lambda_handler(_event(token="forged"), None) == {
        "statusCode": 403,
        "body": "Forbidden",
    }
