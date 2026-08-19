"""The tables the mail import keeps outside the graph.

The graph holds what a message *is*. These six tables hold what we have
*done*: which mailbox we sync, which secret opens it, how far we got, which
job is running, which provider ids are already archived and which we had to
give up on.

Every enum-ish column is a short string rather than a database enum. Adding a
provider, a job kind or a status is then a code change, never a migration.
"""

from datetime import UTC, datetime
from enum import StrEnum

from appkit_commons.database.entities import Base, EncryptedString, Entity
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

ADDRESS_LENGTH = 320
"""RFC 5321 caps a mail address at 64 local + ``@`` + 255 domain characters."""

PROVIDER_ID_LENGTH = 255
"""Provider message ids are opaque strings; none of the providers exceed this."""


class AccountStatus(StrEnum):
    """Where an account stands between syncs.

    ``AUTH_ERROR`` is the one the UI acts on: it offers re-consent instead of
    a retry, because no amount of retrying fixes a revoked token.
    """

    IDLE = "idle"
    SYNCING = "syncing"
    AUTH_ERROR = "auth_error"
    ERROR = "error"


class CredentialKind(StrEnum):
    """How an account authenticates — OAuth today, app passwords for IMAP."""

    OAUTH = "oauth"
    PASSWORD = "password"  # noqa: S105 — names a kind of credential, is not one


class SyncJobKind(StrEnum):
    """What a job does. ``app/worker.py`` maps each kind to its handler."""

    IMPORT = "import"
    INCREMENTAL = "incremental"
    DERIVE = "derive"
    EMBED = "embed"


class SyncJobState(StrEnum):
    """The job lifecycle: ``queued`` → ``running`` → one of the three ends."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MailAccountEntity(Entity, Base):
    """A mailbox this archive syncs from.

    ``provider`` holds the short source name (``gmail``, ``imap``, …) as a
    plain string. The domain names the values; the table deliberately does not
    know them, so a new provider costs no migration here.

    ``enabled`` is the human's switch, ``status`` is the machine's report —
    a disabled account keeps whatever status its last run left behind.
    """

    __tablename__ = "mail_accounts"
    __table_args__ = (
        UniqueConstraint(
            "provider", "email_address", name="uq_mail_accounts_provider_address"
        ),
    )

    provider: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(255))
    email_address: Mapped[str] = mapped_column(String(ADDRESS_LENGTH))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default=AccountStatus.IDLE)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class MailCredentialEntity(Entity, Base):
    """What opens an account, encrypted at rest.

    ``secret`` is deliberately structureless: each provider serialises its own
    pydantic model into it (Gmail its ``client_id``/``client_secret``/
    ``refresh_token``, IMAP a host and a password). That is the
    anti-corruption layer at the persistence boundary — a new provider brings
    its own model and needs **no** migration.
    """

    __tablename__ = "mail_credentials"
    __table_args__ = (
        UniqueConstraint("account_id", "kind", name="uq_mail_credentials_account_kind"),
    )

    account_id: Mapped[int] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    secret: Mapped[str] = mapped_column(EncryptedString)


class MailSyncCheckpointEntity(Entity, Base):
    """How far one sync got, so the next one resumes instead of restarting.

    ``cursor`` is opaque to everything but the provider that wrote it — Gmail
    puts a ``historyId`` in it, IMAP a ``UIDVALIDITY/UIDNEXT`` pair. ``scope``
    names the part of the mailbox it belongs to, because a provider may hand
    out one cursor per label or folder.
    """

    __tablename__ = "mail_sync_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "scope", name="uq_mail_sync_checkpoints_account_scope"
        ),
    )

    account_id: Mapped[int] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(128))
    cursor: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    messages_seen: Mapped[int] = mapped_column(Integer, default=0)


class MailSyncJobEntity(Entity, Base):
    """One unit of work the worker process claims and reports on.

    ``worker_id`` plus ``lease_until`` is what makes a crashed run recoverable
    without a lock server: the next worker claims a job whose lease has run
    out. ``cancel_requested`` is read between batches — a job is asked to
    stop, never killed mid-write.

    ``account_id`` is optional because ``derive`` and ``embed`` work on the
    whole archive rather than on one mailbox.
    """

    __tablename__ = "mail_sync_jobs"

    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(
        String(32), default=SyncJobState.QUEUED, index=True
    )
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="CASCADE"), index=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    messages_total: Mapped[int] = mapped_column(Integer, default=0)
    messages_done: Mapped[int] = mapped_column(Integer, default=0)
    messages_failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MailArchivedMessageEntity(Entity, Base):
    """A read model over the graph: provider ids we have already archived.

    The graph cannot answer "do I know provider id X for account Y?" cheaply
    for a whole batch; a relational ``IN (…)`` can. Nothing but speed lives
    here, so the table stays discardable — it can be rebuilt from the graph at
    any time.
    """

    __tablename__ = "mail_archived_messages"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "provider_message_id",
            name="uq_mail_archived_messages_account_message",
        ),
    )

    account_id: Mapped[int] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="CASCADE"), index=True
    )
    provider_message_id: Mapped[str] = mapped_column(String(PROVIDER_ID_LENGTH))
    # Unbounded on purpose. A canonical id is either a normalised RFC 5322
    # Message-ID, whose length nobody but the sender controls, or the
    # ``sha256:`` fallback, which is 71 characters — so any VARCHAR small
    # enough to look tidy is one a real mailbox eventually overflows. SQLite
    # ignores the length anyway; PostgreSQL would have raised on the first
    # message that arrived without a Message-ID.
    canonical_id: Mapped[str] = mapped_column(Text, index=True)
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class MailFailedMessageEntity(Entity, Base):
    """A message we gave up on, and why.

    Skipping is allowed, silence is not: every message the import drops leaves
    a row here, so a permanent failure is countable rather than invisible.
    """

    __tablename__ = "mail_failed_messages"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="CASCADE"), index=True
    )
    provider_message_id: Mapped[str] = mapped_column(String(PROVIDER_ID_LENGTH))
    reason: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
