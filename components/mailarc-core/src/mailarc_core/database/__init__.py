"""The relational store the application keeps its own tables in.

Everything the import has *done* lives here; everything a message *is* lives
in the graph. One module per concern:

``sqlite``
    The async/sync URL split and the connection pragmas that appkit_commons'
    single database URL cannot express.
``entities``
    The six tables, as SQLAlchemy models. No queries.
``repositories``
    The lookups the engine and the job queue need beyond appkit's CRUD.
"""

from mailarc_core.database.entities import (
    AccountStatus,
    CredentialKind,
    MailAccountEntity,
    MailArchivedMessageEntity,
    MailCredentialEntity,
    MailFailedMessageEntity,
    MailSyncCheckpointEntity,
    MailSyncJobEntity,
    SyncJobKind,
    SyncJobState,
)
from mailarc_core.database.repositories import (
    ArchivedMessageRepository,
    FailedMessageRepository,
    MailAccountRepository,
    MailCredentialRepository,
    SyncCheckpointRepository,
    SyncJobRepository,
)
from mailarc_core.database.sqlite import (
    database_path,
    ensure_database_directory,
    install_pragmas,
    prepare,
    sync_database_url,
)

__all__ = [
    "AccountStatus",
    "ArchivedMessageRepository",
    "CredentialKind",
    "FailedMessageRepository",
    "MailAccountEntity",
    "MailAccountRepository",
    "MailArchivedMessageEntity",
    "MailCredentialEntity",
    "MailCredentialRepository",
    "MailFailedMessageEntity",
    "MailSyncCheckpointEntity",
    "MailSyncJobEntity",
    "SyncCheckpointRepository",
    "SyncJobKind",
    "SyncJobRepository",
    "SyncJobState",
    "database_path",
    "ensure_database_directory",
    "install_pragmas",
    "prepare",
    "sync_database_url",
]
