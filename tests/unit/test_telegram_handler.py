"""Tests for pure helpers and admin command handler in telegram_handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.bot import telegram_handler
from src.bot.config import settings
from src.bot.telegram_handler import _extract_text, _parse_mode, handle_admin_command

# ── _parse_mode ───────────────────────────────────────────────────────────────


def test_parse_mode_returns_html_for_html_content() -> None:
    assert _parse_mode("<b>bold</b>") == "HTML"


def test_parse_mode_returns_none_for_plain_text() -> None:
    assert _parse_mode("just plain text") is None


def test_parse_mode_returns_none_for_empty_string() -> None:
    assert _parse_mode("") is None


# ── _extract_text ─────────────────────────────────────────────────────────────


def test_extract_text_passes_through_plain_string() -> None:
    assert _extract_text("hello") == "hello"


def test_extract_text_extracts_text_blocks_from_list() -> None:
    content = [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "x", "name": "fn", "input": {}},
        {"type": "text", "text": "world"},
    ]
    assert _extract_text(content) == "hello\nworld"


def test_extract_text_returns_empty_string_for_list_with_no_text_blocks() -> None:
    content = [{"type": "tool_use", "id": "x", "name": "fn", "input": {}}]
    assert _extract_text(content) == ""


def test_extract_text_returns_empty_string_for_empty_list() -> None:
    assert _extract_text([]) == ""


# ── handle_admin_command ──────────────────────────────────────────────────────


def _make_update(user_id: int = 35153600) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.message = AsyncMock()
    return update


def _make_context(*args: str) -> MagicMock:
    context = MagicMock()
    context.args = list(args)
    return context


async def test_admin_command_ignored_for_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update(user_id=99999)
    await handle_admin_command(update, _make_context("list"))
    update.message.reply_text.assert_not_awaited()


async def test_admin_command_no_args_shows_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    await handle_admin_command(update, _make_context())
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/auth list" in text


async def test_admin_command_list_no_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    with patch.object(telegram_handler, "scan_by_pk_prefix", return_value=[]):
        await handle_admin_command(update, _make_context("list"))
    update.message.reply_text.assert_awaited_once()
    assert "No auth records" in update.message.reply_text.call_args[0][0]


async def test_admin_command_list_shows_header_and_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    records = [
        {
            "PK": "AUTH#111",
            "status": "APPROVED",
            "entity_type": "USER",
            "username": "@alice",
        },
        {
            "PK": "AUTH#-222",
            "status": "PENDING",
            "entity_type": "GROUP",
            "username": "Trip Group",
        },
    ]
    with patch.object(telegram_handler, "scan_by_pk_prefix", return_value=records):
        await handle_admin_command(update, _make_context("list"))
    text = update.message.reply_text.call_args[0][0]
    assert "Status | Type | Name (ID)" in text
    assert "APPROVED" in text
    assert "PENDING" in text


async def test_admin_command_approve_missing_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    await handle_admin_command(update, _make_context("approve"))
    text = update.message.reply_text.call_args[0][0]
    assert "Usage" in text


async def test_admin_command_approve_updates_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    with patch.object(telegram_handler, "update_item") as mock_update:
        await handle_admin_command(update, _make_context("approve", "111"))
    assert mock_update.call_args[0][2]["status"] == "APPROVED"
    update.message.reply_text.assert_awaited_once()


async def test_admin_command_reject_updates_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    with patch.object(telegram_handler, "update_item") as mock_update:
        await handle_admin_command(update, _make_context("reject", "111"))
    assert mock_update.call_args[0][2]["status"] == "REJECTED"


async def test_admin_command_approve_nonexistent_id_replies_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
        "UpdateItem",
    )
    with patch.object(telegram_handler, "update_item", side_effect=error):
        await handle_admin_command(update, _make_context("approve", "999"))
    text = update.message.reply_text.call_args[0][0]
    assert "No auth record" in text


async def test_admin_command_delete_calls_delete_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    with patch.object(telegram_handler, "delete_item") as mock_delete:
        await handle_admin_command(update, _make_context("delete", "111"))
    mock_delete.assert_called_once_with("AUTH#111", "PROFILE")
    update.message.reply_text.assert_awaited_once()


async def test_admin_command_unknown_subcommand_shows_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    await handle_admin_command(update, _make_context("banana"))
    text = update.message.reply_text.call_args[0][0]
    assert "Unknown" in text


async def test_admin_command_no_message_returns_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    update.message = None
    await handle_admin_command(update, _make_context("list"))
    # No exception raised, nothing to assert — just verifying it doesn't crash


async def test_admin_command_approve_reraises_unexpected_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": ""}},
        "UpdateItem",
    )
    with (
        patch.object(telegram_handler, "update_item", side_effect=error),
        pytest.raises(ClientError),
    ):
        await handle_admin_command(update, _make_context("approve", "111"))
