"""Recognise Telegram updates that have already been delivered once.

Telegram resends an update whenever the webhook does not acknowledge it in time, and the
HTTP API gives up on the Lambda after 30 seconds — so a slow turn is delivered again, to
a fresh invocation that would process it from the start and, for an expense, record it
twice. Every update carries an `update_id` that Telegram assigns uniquely and repeats
unchanged on redelivery, which makes it the key for recognising a repeat.
"""

from datetime import datetime, timezone
from typing import Final

from src.bot.config import settings
from src.bot.storage import dynamodb

UPDATE_PK_PREFIX: Final = "UPDATE#"
UPDATE_MARKER_SK: Final = "MARKER"


def claim_update(update_id: int) -> bool:
    """Record that an update is being handled, unless it already has been.

    Writes an `UPDATE#<update_id>` marker that only one delivery of the update can
    create, and that DynamoDB expires after UPDATE_DEDUP_TTL_SECONDS. Delivery is
    therefore at most once: if the first attempt fails partway, its redelivery is
    dropped too. That is deliberate — a lost message is visible to the user, who sees no
    reply and resends it, where a duplicate expense is silent.

    Args:
        update_id: The `update_id` of the incoming Telegram update.

    Returns:
        True if this call claimed the update and it should be processed, False if the
        update was already claimed by an earlier delivery.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails. The update is
            then neither claimed nor processed, and the failed invocation leads Telegram
            to deliver it again.
    """
    now = datetime.now(timezone.utc)
    return dynamodb.put_item_if_absent(
        {
            "PK": f"{UPDATE_PK_PREFIX}{update_id}",
            "SK": UPDATE_MARKER_SK,
            "claimed_at": now.isoformat(timespec="microseconds"),
            dynamodb.TTL_ATTRIBUTE: int(now.timestamp())
            + settings.UPDATE_DEDUP_TTL_SECONDS,
        }
    )
