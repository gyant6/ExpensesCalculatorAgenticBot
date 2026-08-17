"""Access-control vocabulary shared by the Telegram handlers and the auth records.

Every value that is persisted to DynamoDB or parsed back off the wire is defined here
rather than written inline at each use site, so a rename cannot leave one call site
comparing against a string nothing else writes any more.

All three enums subclass `StrEnum`, which means members compare equal to their plain
string form and boto3's `TypeSerializer` stores them as a DynamoDB String. Records
written before these enums existed therefore keep matching without a migration.
"""

from enum import StrEnum
from typing import Final

# Partition key prefix and sort key for an access-control record. The full primary key
# for an entity is (f"{AUTH_PK_PREFIX}{auth_id}", AUTH_SK).
AUTH_PK_PREFIX: Final = "AUTH#"
AUTH_SK: Final = "PROFILE"


class AuthStatus(StrEnum):
    """Lifecycle state of an access-control record, stored in the `status` attribute.

    Members:
        PENDING: The entity has contacted the bot and the admin has been notified, but
            has not yet decided. Requests are refused while in this state.
        APPROVED: The entity may use the bot. This is the only state that grants access.
        REJECTED: The admin refused the request. Distinguished from PENDING so a refused
            entity is never re-sent to the admin.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class EntityType(StrEnum):
    """Whether an access-control record describes a person or a group chat.

    Stored in the `entity_type` attribute and used only for display. Telegram already
    distinguishes the two by sign — user IDs are positive, group chat IDs negative — so
    this attribute never decides access on its own.
    """

    USER = "USER"
    GROUP = "GROUP"


class AuthCommand(StrEnum):
    """Subcommands accepted by the admin-only `/auth` command.

    APPROVE and REJECT double as the action segment of the inline-keyboard callback
    data, so the button a tap produces and the subcommand typed by hand resolve to the
    same member.

    Members:
        LIST: Print every access-control record with its status, type, name and ID.
        APPROVE: Set the record's status to APPROVED.
        REJECT: Set the record's status to REJECTED.
        DELETE: Remove the record entirely so the entity can request access afresh.
    """

    LIST = "list"
    APPROVE = "approve"
    REJECT = "reject"
    DELETE = "delete"


# The status each reviewing command assigns. Keyed by command so the inline keyboard and
# the typed subcommand cannot drift apart on what "approve" means.
REVIEW_STATUS: Final[dict[AuthCommand, AuthStatus]] = {
    AuthCommand.APPROVE: AuthStatus.APPROVED,
    AuthCommand.REJECT: AuthStatus.REJECTED,
}
