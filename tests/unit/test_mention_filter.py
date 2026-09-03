"""Tests for ignoring group messages aimed at other people.

Privacy mode is off, so the bot receives every group message and would otherwise send
each one to the model — paying for a Bedrock call to answer a conversation between two
other members.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from telegram import Bot, Message, MessageEntity, User

from src.bot.telegram_handler import _addressed_to_someone_else

_BOT_ID = 7743217744
_BOT_NAME = "ZuzuAssistantBot"


@pytest.fixture
def bot() -> MagicMock:
    stub = MagicMock(spec=Bot)
    stub.id = _BOT_ID
    stub.username = _BOT_NAME
    return stub


def _message(text: str, entities: list[MessageEntity] | None = None) -> Message:
    return Message(
        message_id=1,
        date=None,  # type: ignore[arg-type]
        chat=MagicMock(),
        text=text,
        entities=entities or [],
    )


def _mention(text: str, at: str) -> Message:
    offset = text.index(at)
    return _message(
        text,
        [MessageEntity(type=MessageEntity.MENTION, offset=offset, length=len(at))],
    )


def test_a_message_tagging_another_member_is_ignored(bot: MagicMock) -> None:
    assert _addressed_to_someone_else(_mention("@peilin can you pay?", "@peilin"), bot)


def test_a_message_tagging_the_bot_is_handled(bot: MagicMock) -> None:
    message = _mention(f"@{_BOT_NAME} lunch $12", f"@{_BOT_NAME}")

    assert _addressed_to_someone_else(message, bot) is False


def test_tagging_the_bot_alongside_others_is_still_addressed(bot: MagicMock) -> None:
    text = f"@peilin and @{_BOT_NAME} split this"
    entities = [
        MessageEntity(
            type=MessageEntity.MENTION, offset=text.index("@peilin"), length=7
        ),
        MessageEntity(
            type=MessageEntity.MENTION,
            offset=text.index(f"@{_BOT_NAME}"),
            length=len(_BOT_NAME) + 1,
        ),
    ]

    assert _addressed_to_someone_else(_message(text, entities), bot) is False


def test_a_mention_of_the_bot_is_matched_case_insensitively(bot: MagicMock) -> None:
    lowered = _BOT_NAME.lower()
    assert (
        _addressed_to_someone_else(_mention(f"@{lowered} hi", f"@{lowered}"), bot)
        is False
    )


def test_an_ordinary_expense_message_is_handled(bot: MagicMock) -> None:
    assert _addressed_to_someone_else(_message("lunch $12"), bot) is False


def test_an_email_address_is_not_a_mention(bot: MagicMock) -> None:
    # Telegram tags this as an EMAIL entity, never a MENTION — which is why the check
    # reads entities rather than searching the text for "@".
    text = "receipt sent to pei@example.com"
    entities = [
        MessageEntity(type=MessageEntity.EMAIL, offset=text.index("pei@"), length=19)
    ]

    assert _addressed_to_someone_else(_message(text, entities), bot) is False


def test_offsets_survive_an_emoji_before_a_mention_of_the_bot(bot: MagicMock) -> None:
    # Telegram counts offsets in UTF-16 code units and an emoji occupies two, so a plain
    # Python slice starts one character late and reads a name that matches nothing. The
    # bot would then look like somebody else and its own message would be ignored — which
    # only shows up when the mention is of the bot, since a misread of any other name is
    # wrong in a way that happens to give the same answer.
    text = f"🎉 @{_BOT_NAME} lunch $12"
    entities = [
        MessageEntity(type=MessageEntity.MENTION, offset=3, length=len(_BOT_NAME) + 1)
    ]

    assert _addressed_to_someone_else(_message(text, entities), bot) is False


def test_offsets_survive_an_emoji_before_someone_elses_mention(bot: MagicMock) -> None:
    text = "🎉 @peilin your turn"
    entities = [MessageEntity(type=MessageEntity.MENTION, offset=3, length=7)]

    assert _addressed_to_someone_else(_message(text, entities), bot)


def test_a_text_mention_of_another_user_is_ignored(bot: MagicMock) -> None:
    # Someone with no username: Telegram sends the user object instead of an @handle.
    other = User(id=797268440, first_name="pei lin", is_bot=False)
    entities = [
        MessageEntity(type=MessageEntity.TEXT_MENTION, offset=0, length=7, user=other)
    ]

    assert _addressed_to_someone_else(_message("pei lin pay up", entities), bot)


def test_a_text_mention_of_the_bot_is_handled(bot: MagicMock) -> None:
    itself = User(id=_BOT_ID, first_name="Zuzu", is_bot=True)
    entities = [
        MessageEntity(type=MessageEntity.TEXT_MENTION, offset=0, length=4, user=itself)
    ]

    assert (
        _addressed_to_someone_else(_message("Zuzu lunch $12", entities), bot) is False
    )
