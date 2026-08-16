"""Tests for the access-control gate in telegram_handler."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

from src.bot import telegram_handler
from src.bot.telegram_handler import _check_auth, handle_auth_callback


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_update(
    user_id: int = 111,
    username: str | None = "testuser",
    full_name: str = "Test User",
    chat_type: str = "private",
    chat_id: int = 111,
    chat_title: str | None = None,
) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = username
    update.effective_user.full_name = full_name
    update.effective_chat.type = chat_type
    update.effective_chat.id = chat_id
    update.effective_chat.title = chat_title
    update.message = AsyncMock()
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return context


def _make_callback_query(data: str) -> AsyncMock:
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


# ── _check_auth ───────────────────────────────────────────────────────────────


async def test_first_contact_creates_pending_and_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_handler.settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    context = _make_context()

    with (
        patch.object(telegram_handler, "get_item", return_value=None) as mock_get,
        patch.object(telegram_handler, "put_item") as mock_put,
    ):
        result = await _check_auth(update, context, "111", "USER", "@testuser")

    assert result is False
    mock_get.assert_called_once_with("AUTH#111", "PROFILE")
    written = mock_put.call_args[0][0]
    assert written["PK"] == "AUTH#111"
    assert written["SK"] == "PROFILE"
    assert written["status"] == "PENDING"
    assert written["entity_type"] == "USER"
    assert written["username"] == "@testuser"
    assert "requested_at" in written
    context.bot.send_message.assert_awaited_once_with(
        chat_id=35153600, text=ANY, reply_markup=ANY
    )
    update.message.reply_text.assert_awaited_once()


async def test_first_contact_does_not_create_pending_if_no_reply_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_check_auth should not crash when update.message is None."""
    monkeypatch.setattr(telegram_handler.settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update()
    update.message = None
    context = _make_context()

    with (
        patch.object(telegram_handler, "get_item", return_value=None),
        patch.object(telegram_handler, "put_item"),
    ):
        result = await _check_auth(update, context, "111", "USER", "@testuser")

    assert result is False
    context.bot.send_message.assert_awaited_once()


async def test_pending_returns_false_and_replies_without_notifying_admin() -> None:
    update = _make_update()
    context = _make_context()

    with patch.object(telegram_handler, "get_item", return_value={"status": "PENDING"}):
        result = await _check_auth(update, context, "111", "USER", "@testuser")

    assert result is False
    update.message.reply_text.assert_awaited_once()
    context.bot.send_message.assert_not_awaited()


async def test_approved_returns_true_without_any_message() -> None:
    update = _make_update()
    context = _make_context()

    with patch.object(telegram_handler, "get_item", return_value={"status": "APPROVED"}):
        result = await _check_auth(update, context, "111", "USER", "@testuser")

    assert result is True
    update.message.reply_text.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()


async def test_rejected_returns_false_and_replies_without_notifying_admin() -> None:
    update = _make_update()
    context = _make_context()

    with patch.object(telegram_handler, "get_item", return_value={"status": "REJECTED"}):
        result = await _check_auth(update, context, "111", "USER", "@testuser")

    assert result is False
    update.message.reply_text.assert_awaited_once()
    context.bot.send_message.assert_not_awaited()


async def test_group_uses_negative_chat_id_as_auth_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_handler.settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update(
        chat_type="group", chat_id=-100123, chat_title="My Trip Group"
    )
    context = _make_context()

    with (
        patch.object(telegram_handler, "get_item", return_value=None) as mock_get,
        patch.object(telegram_handler, "put_item") as mock_put,
    ):
        result = await _check_auth(
            update, context, "-100123", "GROUP", "My Trip Group"
        )

    assert result is False
    mock_get.assert_called_once_with("AUTH#-100123", "PROFILE")
    written = mock_put.call_args[0][0]
    assert written["PK"] == "AUTH#-100123"
    assert written["entity_type"] == "GROUP"


async def test_username_falls_back_to_full_name_when_no_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_handler.settings, "ADMIN_TELEGRAM_ID", 35153600)
    update = _make_update(username=None, full_name="Jane Doe")
    context = _make_context()

    with (
        patch.object(telegram_handler, "get_item", return_value=None),
        patch.object(telegram_handler, "put_item") as mock_put,
    ):
        await _check_auth(update, context, "111", "USER", "Jane Doe")

    written = mock_put.call_args[0][0]
    assert written["username"] == "Jane Doe"


# ── handle_auth_callback ──────────────────────────────────────────────────────


async def test_approve_updates_status_and_notifies_requester() -> None:
    query = _make_callback_query("auth:approve:111")
    update = MagicMock()
    update.callback_query = query
    context = _make_context()

    auth_record = {"status": "PENDING", "entity_type": "USER", "username": "@testuser"}

    with (
        patch.object(telegram_handler, "get_item", return_value=auth_record),
        patch.object(telegram_handler, "update_item") as mock_update,
    ):
        await handle_auth_callback(update, context)

    updated_fields = mock_update.call_args[0][2]
    assert updated_fields["status"] == "APPROVED"
    assert "reviewed_at" in updated_fields
    context.bot.send_message.assert_awaited_once_with(chat_id=111, text=ANY)
    query.edit_message_text.assert_awaited_once()
    # Confirm the edit shows "Approved" not "Rejected"
    edit_text = query.edit_message_text.call_args[0][0]
    assert "Approved" in edit_text


async def test_reject_updates_status_and_notifies_requester() -> None:
    query = _make_callback_query("auth:reject:111")
    update = MagicMock()
    update.callback_query = query
    context = _make_context()

    auth_record = {"status": "PENDING", "entity_type": "USER", "username": "@testuser"}

    with (
        patch.object(telegram_handler, "get_item", return_value=auth_record),
        patch.object(telegram_handler, "update_item") as mock_update,
    ):
        await handle_auth_callback(update, context)

    updated_fields = mock_update.call_args[0][2]
    assert updated_fields["status"] == "REJECTED"
    assert "reviewed_at" in updated_fields
    context.bot.send_message.assert_awaited_once_with(chat_id=111, text=ANY)
    edit_text = query.edit_message_text.call_args[0][0]
    assert "Rejected" in edit_text


async def test_auth_callback_missing_record_edits_message_without_notifying() -> None:
    query = _make_callback_query("auth:approve:999")
    update = MagicMock()
    update.callback_query = query
    context = _make_context()

    with patch.object(telegram_handler, "get_item", return_value=None):
        await handle_auth_callback(update, context)

    query.edit_message_text.assert_awaited_once()
    context.bot.send_message.assert_not_awaited()


async def test_auth_callback_no_query_returns_early() -> None:
    update = MagicMock()
    update.callback_query = None
    context = _make_context()

    # Should not raise, and should not call anything
    await handle_auth_callback(update, context)
    context.bot.send_message.assert_not_awaited()


async def test_auth_callback_approve_group_notifies_group_chat() -> None:
    """Approving a group sends the notification to the group chat (negative ID)."""
    query = _make_callback_query("auth:approve:-100123")
    update = MagicMock()
    update.callback_query = query
    context = _make_context()

    auth_record = {
        "status": "PENDING",
        "entity_type": "GROUP",
        "username": "My Trip Group",
    }

    with (
        patch.object(telegram_handler, "get_item", return_value=auth_record),
        patch.object(telegram_handler, "update_item"),
    ):
        await handle_auth_callback(update, context)

    context.bot.send_message.assert_awaited_once_with(chat_id=-100123, text=ANY)
