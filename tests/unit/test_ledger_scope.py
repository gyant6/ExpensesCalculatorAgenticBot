"""Tests that a group's expenses belong to the group rather than to whoever typed them.

The bug these pin: authorisation approved a group as one entity, but storage keyed on the
sender, so every member got a private trip inside a shared conversation. Two people in one
group each saw their own expenses and neither saw the other's.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.bot.telegram_handler import _ledger_id_for

_GROUP_ID = -5471148436
_ALICE = 35153600
_BOB = 797268440


def _update(chat_id: int, chat_type: str, sender_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_user.id = sender_id
    return update


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_every_member_of_a_group_shares_one_ledger(chat_type: str) -> None:
    alice = _ledger_id_for(_update(_GROUP_ID, chat_type, _ALICE))
    bob = _ledger_id_for(_update(_GROUP_ID, chat_type, _BOB))

    assert alice == bob == str(_GROUP_ID)


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_a_group_ledger_is_never_the_sender(chat_type: str) -> None:
    # The regression itself: keying on the sender is what split the ledger in two.
    assert _ledger_id_for(_update(_GROUP_ID, chat_type, _ALICE)) != str(_ALICE)


def test_a_private_chat_is_scoped_to_the_user() -> None:
    # Telegram gives a private chat the same ID as its user, so this is the user's ledger.
    assert _ledger_id_for(_update(_ALICE, "private", _ALICE)) == str(_ALICE)


def test_the_same_person_keeps_separate_private_and_group_ledgers() -> None:
    private = _ledger_id_for(_update(_ALICE, "private", _ALICE))
    group = _ledger_id_for(_update(_GROUP_ID, "supergroup", _ALICE))

    assert private != group


def test_an_update_with_no_chat_has_no_ledger() -> None:
    update = MagicMock()
    update.effective_chat = None

    assert _ledger_id_for(update) is None
