"""The queries the import needs on top of appkit's CRUD.

``BaseRepository`` already brings ``create``, ``find_by_id``, ``find_all``,
``update``, ``save``, ``count`` and the deletes; none of that is repeated here.
What is here are the lookups the engine and the job queue cannot express with
a primary key.

None of these methods commits. ``get_asyncdb_session()`` owns the transaction
boundary and commits when its block leaves cleanly.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from appkit_commons.database.base_repository import BaseRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mailarc_core.database.entities import (
    MailAccountEntity,
    MailArchivedMessageEntity,
    MailCredentialEntity,
    MailFailedMessageEntity,
    MailSyncCheckpointEntity,
    MailSyncJobEntity,
    SyncJobState,
)

logger = logging.getLogger(__name__)


class MailAccountRepository(BaseRepository[MailAccountEntity, AsyncSession]):
    """Accounts, addressed the way a human names them."""

    @property
    def model_class(self) -> type[MailAccountEntity]:
        return MailAccountEntity

    async def find_by_address(
        self, session: AsyncSession, provider: str, email_address: str
    ) -> MailAccountEntity | None:
        """Find the one account for a provider and address — the natural key."""
        result = await session.execute(
            select(MailAccountEntity).where(
                MailAccountEntity.provider == provider,
                MailAccountEntity.email_address == email_address,
            )
        )
        return result.scalars().first()

    async def find_enabled(self, session: AsyncSession) -> list[MailAccountEntity]:
        """The accounts a scheduled run may touch."""
        result = await session.execute(
            select(MailAccountEntity)
            .where(MailAccountEntity.enabled.is_(True))
            .order_by(MailAccountEntity.id)
        )
        return list(result.scalars().all())


class MailCredentialRepository(BaseRepository[MailCredentialEntity, AsyncSession]):
    """Secrets, decrypted on read by :class:`EncryptedString`."""

    @property
    def model_class(self) -> type[MailCredentialEntity]:
        return MailCredentialEntity

    async def find_by_account(
        self, session: AsyncSession, account_id: int, kind: str
    ) -> MailCredentialEntity | None:
        """Find the account's credential of one kind; there is at most one."""
        result = await session.execute(
            select(MailCredentialEntity).where(
                MailCredentialEntity.account_id == account_id,
                MailCredentialEntity.kind == kind,
            )
        )
        return result.scalars().first()


class SyncCheckpointRepository(BaseRepository[MailSyncCheckpointEntity, AsyncSession]):
    """Where the last run stopped, per account and scope."""

    @property
    def model_class(self) -> type[MailSyncCheckpointEntity]:
        return MailSyncCheckpointEntity

    async def find_by_account_and_scope(
        self, session: AsyncSession, account_id: int, scope: str
    ) -> MailSyncCheckpointEntity | None:
        """Find the checkpoint a run should resume from."""
        result = await session.execute(
            select(MailSyncCheckpointEntity).where(
                MailSyncCheckpointEntity.account_id == account_id,
                MailSyncCheckpointEntity.scope == scope,
            )
        )
        return result.scalars().first()

    async def upsert_cursor(
        self,
        session: AsyncSession,
        account_id: int,
        scope: str,
        cursor: str | None,
        messages_seen: int,
    ) -> MailSyncCheckpointEntity:
        """Advance the checkpoint, creating it on the first run of a scope.

        ``messages_seen`` is the run's own total, not a delta: the caller
        checkpoints every few hundred messages and knows how many it has seen.
        """
        checkpoint = await self.find_by_account_and_scope(session, account_id, scope)
        if checkpoint is None:
            checkpoint = MailSyncCheckpointEntity(account_id=account_id, scope=scope)
            session.add(checkpoint)
        checkpoint.cursor = cursor
        checkpoint.messages_seen = messages_seen
        checkpoint.last_run_at = datetime.now(UTC)
        await session.flush()
        logger.debug("Checkpoint %d/%s advanced to %s", account_id, scope, cursor)
        return checkpoint


class SyncJobRepository(BaseRepository[MailSyncJobEntity, AsyncSession]):
    """Plain job lookups.

    Claiming, the heartbeat and the cancel are conditional ``UPDATE``s whose
    correctness lives in their ``WHERE`` clause; they belong to the job queue
    in ``mailarc-sync``, not to a finder.
    """

    @property
    def model_class(self) -> type[MailSyncJobEntity]:
        return MailSyncJobEntity

    async def find_queued(self, session: AsyncSession) -> list[MailSyncJobEntity]:
        """The jobs waiting for a worker, oldest first."""
        return await self.find_by_state(session, SyncJobState.QUEUED)

    async def find_by_state(
        self, session: AsyncSession, state: str
    ) -> list[MailSyncJobEntity]:
        """All jobs in one state, oldest first."""
        result = await session.execute(
            select(MailSyncJobEntity)
            .where(MailSyncJobEntity.state == state)
            .order_by(MailSyncJobEntity.id)
        )
        return list(result.scalars().all())

    async def find_running_for_account(
        self, session: AsyncSession, account_id: int
    ) -> list[MailSyncJobEntity]:
        """What is currently running against one mailbox.

        The UI asks before offering a sync, so a human is not told to wait by
        a constraint violation.
        """
        result = await session.execute(
            select(MailSyncJobEntity)
            .where(
                MailSyncJobEntity.account_id == account_id,
                MailSyncJobEntity.state == SyncJobState.RUNNING,
            )
            .order_by(MailSyncJobEntity.id)
        )
        return list(result.scalars().all())


class ArchivedMessageRepository(
    BaseRepository[MailArchivedMessageEntity, AsyncSession]
):
    """The read model that keeps the import from re-fetching what it has."""

    @property
    def model_class(self) -> type[MailArchivedMessageEntity]:
        return MailArchivedMessageEntity

    async def find_known_provider_ids(
        self, session: AsyncSession, account_id: int, provider_ids: Sequence[str]
    ) -> set[str]:
        """Return the subset of ``provider_ids`` already archived for an account.

        One statement per listing batch. The caller subtracts the result from
        the batch and fetches only the difference.
        """
        if not provider_ids:
            return set()
        result = await session.execute(
            select(MailArchivedMessageEntity.provider_message_id).where(
                MailArchivedMessageEntity.account_id == account_id,
                MailArchivedMessageEntity.provider_message_id.in_(provider_ids),
            )
        )
        return set(result.scalars().all())

    async def record_many(
        self,
        session: AsyncSession,
        account_id: int,
        canonical_by_provider_id: Mapping[str, str],
        archived_at: datetime | None = None,
    ) -> list[MailArchivedMessageEntity]:
        """Note a whole batch as archived, under one timestamp.

        A mapping rather than pairs, because both halves are opaque ids and a
        swapped tuple would go unnoticed. ``save_all`` is not used: it refreshes
        every row it just wrote, which for a few hundred rows is a second round
        trip for nothing.
        """
        stamped_at = archived_at or datetime.now(UTC)
        rows = [
            MailArchivedMessageEntity(
                account_id=account_id,
                provider_message_id=provider_message_id,
                canonical_id=canonical_id,
                archived_at=stamped_at,
            )
            for provider_message_id, canonical_id in canonical_by_provider_id.items()
        ]
        session.add_all(rows)
        await session.flush()
        logger.debug("Archived %d messages for account %d", len(rows), account_id)
        return rows


class FailedMessageRepository(BaseRepository[MailFailedMessageEntity, AsyncSession]):
    """The other half of the ledger: what the import could not take."""

    @property
    def model_class(self) -> type[MailFailedMessageEntity]:
        return MailFailedMessageEntity

    async def record(
        self,
        session: AsyncSession,
        account_id: int,
        provider_message_id: str,
        reason: str,
        detail: str | None = None,
    ) -> MailFailedMessageEntity:
        """Leave a row for a skipped message.

        ``reason`` is the error taxonomy's short name; ``detail`` carries the
        message a human needs to judge it. Skipping without calling this is
        the one thing the import may never do.
        """
        row = MailFailedMessageEntity(
            account_id=account_id,
            provider_message_id=provider_message_id,
            reason=reason,
            detail=detail,
            occurred_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        logger.warning(
            "Skipped message %s of account %d: %s",
            provider_message_id,
            account_id,
            reason,
        )
        return row
