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
from typing import Any, cast

from appkit_commons.database.base_repository import BaseRepository
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from mailarc_core.database.entities import (
    SEMANTIC_SETTINGS_ID,
    MailAccountEntity,
    MailArchivedMessageEntity,
    MailCredentialEntity,
    MailFailedMessageEntity,
    MailSyncCheckpointEntity,
    MailSyncJobEntity,
    SemanticSettingsEntity,
    SyncJobState,
)

logger = logging.getLogger(__name__)

FAILURE_LIMIT = 20
"""How many recent failures a panel asks for when it does not say."""

MAX_FAILURE_ROWS = 500
"""The ceiling on both recent-failure listings.

:meth:`FailedMessageRepository.find_recent` and
:meth:`SyncJobRepository.find_recent_failed` share it because they share their
reader: the notification panel puts their answers on one list. Clamped at the
top as well as the bottom for the reason ``mailarc_analytics``' ``_limit`` is:
the ledger of skipped messages grows with every import and has no natural size,
so an unbounded ``find_recent(limit=10_000_000)`` would pull the whole table
into a list to render twenty lines of it. A panel is a panel.
"""


async def _deleted(session: AsyncSession, statement: Any) -> int:
    """Run a bulk ``DELETE`` and say how many rows it took.

    ``AsyncSession.execute`` is typed as returning a plain ``Result``; a
    ``DELETE`` always yields a ``CursorResult``, which is the one that carries
    the count. The same cast :mod:`mailarc_sync.jobs.queue` makes for its
    conditional ``UPDATE``, and for the same reason.

    One statement rather than a load-and-delete loop: the three ledgers this
    serves have a row per message, and the whole point of clearing a mailbox is
    that there are a great many of them.
    """
    result = cast("CursorResult[Any]", await session.execute(statement))
    return result.rowcount or 0


def _capped(value: int) -> int:
    """A page size a listing can actually be bound to.

    ``LIMIT 0`` is legal SQL that returns nothing, so a caller's stray nought
    would render a healthy archive rather than the mistake it is. One row is
    the smallest answer that still says something.
    """
    return min(max(1, value), MAX_FAILURE_ROWS)


class ApiKeyNotStored(Exception):
    """The embedder key could not be written, said without quoting the key.

    Its own type rather than a re-raised ``StatementError``, because the point
    is what it does *not* carry: see
    :meth:`SemanticSettingsRepository.set_api_key`. A caller may log this one.
    """


class SettingsChangedElsewhere(Exception):
    """The row moved under the caller since they read it, so the write is refused.

    The embedder settings are one row for the whole archive and every column is
    overwritten on every save, so two administrators editing the form at the
    same time lose one set of changes with nothing said. What makes that worse
    than an ordinary lost update is *which* settings these are: putting a
    dimension back silently un-does the one change the vector index was just
    migrated for, and the archive then embeds at a length the index does not
    carry — accepted, stored, never indexed and reported nowhere.

    Refused rather than merged, because there is nothing to merge with: the
    stale editor's "ollama" is a statement about a row that no longer exists,
    and a per-column merge would invent an embedder neither of them chose.
    """


class CredentialNotStored(Exception):
    """A mail credential could not be written, said without quoting the secret.

    The sibling of :class:`ApiKeyNotStored`, for the column that carries a
    Gmail refresh token — see :meth:`MailCredentialRepository.store_secret`.
    A caller may log this one.
    """


def _without_parameters(error: StatementError) -> str:
    """The cause of a statement failure, minus the statement and its binds.

    ``str(StatementError)`` is the message plus ``[SQL: …]`` plus
    ``[parameters: …]``, and on either write this guards the parameters are
    the plaintext secret. ``orig`` is the exception the driver or the column
    type actually raised, and neither the Fernet error nor a DBAPI error
    repeats a bind value in its own message.
    """
    cause = error.orig
    if cause is None:
        return "the database refused the write"
    return f"{type(cause).__name__}: {cause}"


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

    async def store_secret(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        kind: str,
        secret: str,
    ) -> MailCredentialEntity:
        """Write an account's secret of one kind, creating the row on the first.

        The one write path for :attr:`MailCredentialEntity.secret`, and having
        one *is* the security property. SQLAlchemy's ``StatementError`` quotes
        the statement **and its bind parameters**, and for this statement the
        bind parameter is the plaintext secret — a Gmail refresh token today.
        Encrypting it is ``EncryptedString``'s bind processing, so the one way
        this write can fail is also the one way the token reaches a log, and
        the callers doing the right thing — the broad ``logger.exception`` in
        ``accounts/state.py``, and ``app/worker.py``'s ``_keep_refreshed_secret``
        insuring the import against a lost rotation — would be the things that
        leaked it. Measured against the agent sandbox, whose configured Fernet
        key was invalid:

            (builtins.ValueError) 'app_database_encryption_key' is not a valid
            32-byte url-safe base64-encoded Fernet key.
            [SQL: INSERT INTO mail_credentials (account_id, kind, secret) …]
            [parameters: [{… 'secret': '{"refresh_token": "1//top-secret"}'}]]

        A ``try``/``except`` at each of those callers would be the same guard
        written three times and forgotten on the fourth; here it cannot be
        skipped, because assigning ``secret`` and flushing by hand is no longer
        something a caller does. ``from None`` is deliberate: chaining would
        put the original back into a rendered traceback.

        The flush is explicit for the same reason. Left to the transaction, the
        failure would surface at commit — outside this method, past the guard,
        and inside the caller's ``except`` block.

        Upsert on ``(account_id, kind)`` because that pair is the table's
        unique constraint: a rotated token replaces the one it was issued
        against rather than colliding with it.
        """
        credential = await self.find_by_account(session, account_id, kind)
        if credential is None:
            credential = MailCredentialEntity(account_id=account_id, kind=kind)
            session.add(credential)
        credential.secret = secret
        try:
            await session.flush()
        except StatementError as error:
            raise CredentialNotStored(_without_parameters(error)) from None
        logger.debug("Stored the %s credential of account %d", kind, account_id)
        return credential


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

    async def delete_for_account(self, session: AsyncSession, account_id: int) -> int:
        """Forget where every scope of this mailbox got to. Returns rows removed.

        What makes a cleared mailbox importable again rather than merely empty.
        A full walk resumes from the page token in ``full``, and a delta from
        the watermark in ``incremental`` — so an archive emptied with these
        rows left standing would re-import nothing at all and report success.
        """
        removed = await _deleted(
            session,
            delete(MailSyncCheckpointEntity).where(
                MailSyncCheckpointEntity.account_id == account_id
            ),
        )
        logger.info("Dropped %d checkpoints of account %d", removed, account_id)
        return removed


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

    async def find_recent_failed(
        self, session: AsyncSession, *, limit: int = FAILURE_LIMIT
    ) -> list[MailSyncJobEntity]:
        """The newest failed jobs first — what a notification panel reads.

        Its own finder rather than :meth:`find_by_state`, and the difference is
        the whole point: ``find_by_state`` carries no ``LIMIT`` and orders by
        id **ascending**, so a panel wanting eight lines out of it loaded every
        job that ever failed, oldest first, and discarded all but the newest
        few — on a page a signed-out visitor can open.

        ``finished_at`` descending with the id as the tie-break, and **nulls
        are kept**: a job killed mid-write never wrote a finish time, and a
        listing that dropped it would hide the worst failure there is. Where
        that column is null the id carries the order, which is the arrival
        order of the queue.

        ``limit`` goes through :func:`_capped`, like every other listing here.
        """
        result = await session.execute(
            select(MailSyncJobEntity)
            .where(MailSyncJobEntity.state == SyncJobState.FAILED)
            .order_by(
                MailSyncJobEntity.finished_at.desc().nulls_last(),
                MailSyncJobEntity.id.desc(),
            )
            .limit(_capped(limit))
        )
        return list(result.scalars().all())

    async def count_by_state(self, session: AsyncSession) -> dict[str, int]:
        """How many jobs sit in each state, counted by the database.

        One ``GROUP BY`` rather than the ``len(await find_queued(session))``
        this replaces: a dashboard wants five numbers, and loading five sets of
        entity rows to measure their length reads the whole job table over the
        wire to throw all of it away. It also gets worse exactly where it
        matters — a long-running archive has tens of thousands of succeeded
        jobs behind the one number nobody looks at.

        A state with no jobs is **absent** from the mapping, because ``GROUP
        BY`` cannot invent a row for it. Callers read it with ``.get(state,
        0)``; making this method fill in the five known states would have it
        answer a question about :class:`SyncJobState` rather than about the
        table.
        """
        result = await session.execute(
            select(MailSyncJobEntity.state, func.count()).group_by(
                MailSyncJobEntity.state
            )
        )
        return dict(result.tuples().all())

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

    async def find_open_for_account(
        self, session: AsyncSession, account_id: int
    ) -> list[MailSyncJobEntity]:
        """Everything against one mailbox that has not finished yet.

        Wider than :meth:`find_running_for_account` by exactly one state, and
        the difference is the point: a caller that is about to *destroy* what a
        job would write has to count the queued one too. A job that is merely
        waiting for a worker is a job that starts the moment the worker frees
        up, which — mid clear-out — is a half-imported mailbox and a ledger
        that disagrees with the graph.
        """
        result = await session.execute(
            select(MailSyncJobEntity)
            .where(
                MailSyncJobEntity.account_id == account_id,
                MailSyncJobEntity.state.in_(
                    (SyncJobState.QUEUED, SyncJobState.RUNNING)
                ),
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

    async def delete_for_account(self, session: AsyncSession, account_id: int) -> int:
        """Forget everything this mailbox has archived. Returns rows removed.

        The other half of what makes a cleared mailbox importable again. The
        import subtracts this table from every listing batch before it fetches
        anything, so rows left here after the graph has been emptied would make
        the next import skip precisely the messages that are no longer in the
        archive.

        One statement rather than a load-and-delete loop: this table has a row
        per message and the whole point is that there are a great many of them.
        """
        removed = await _deleted(
            session,
            delete(MailArchivedMessageEntity).where(
                MailArchivedMessageEntity.account_id == account_id
            ),
        )
        logger.info(
            "Dropped %d archived-message rows of account %d", removed, account_id
        )
        return removed


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

    async def delete_for_account(self, session: AsyncSession, account_id: int) -> int:
        """Drop this mailbox's ledger of skipped messages. Returns rows removed.

        Nothing depends on these rows the way the import depends on the
        archived ones — they are read by a panel, never by the engine — but
        they name provider ids that no longer exist anywhere else once the
        mailbox has been cleared. Keeping them would leave the notifications
        reporting failures against an import that has been undone.
        """
        removed = await _deleted(
            session,
            delete(MailFailedMessageEntity).where(
                MailFailedMessageEntity.account_id == account_id
            ),
        )
        logger.info("Dropped %d failure rows of account %d", removed, account_id)
        return removed

    async def find_recent(
        self, session: AsyncSession, *, limit: int = FAILURE_LIMIT
    ) -> list[MailFailedMessageEntity]:
        """The newest failures first — what a notification panel reads.

        Ordered by ``occurred_at`` descending, with the id as the tie-break:
        the column is written by :meth:`record` from one clock, and a batch
        that skips several messages inside the same tick would otherwise come
        back in whatever order the database felt like. Newest-first is what
        makes the top of the list worth reading.

        ``limit`` goes through :func:`_capped`, so neither a stray nought nor
        a caller asking for the whole ledger gets what it literally asked for.
        """
        result = await session.execute(
            select(MailFailedMessageEntity)
            .order_by(
                MailFailedMessageEntity.occurred_at.desc(),
                MailFailedMessageEntity.id.desc(),
            )
            .limit(_capped(limit))
        )
        return list(result.scalars().all())


class SemanticSettingsRepository(BaseRepository[SemanticSettingsEntity, AsyncSession]):
    """The one embedder settings row, and a write API that cannot leak the key.

    Three writes instead of one, and that split *is* the security property.
    :meth:`store` has no ``api_key`` parameter at all, so "an empty field means
    leave the stored key alone" is not a rule a form has to remember and can
    forget — it is the only thing this class can do. Replacing the key is
    :meth:`set_api_key` and forgetting it is :meth:`clear_api_key`, which is
    the explicit control a write-only secret needs: without it the only way
    back from "a key is stored" would be to type a new one.

    ``store`` rather than ``save``: appkit's ``BaseRepository.save(session,
    entity)`` is inherited and still means what it always meant, and an
    override with a different shape would be a Liskov violation that a caller
    holding the base type would meet at runtime.

    Nothing here logs a key, and :meth:`api_key_is_set` answers the question a
    browser is allowed to ask without the value ever leaving the database.
    """

    @property
    def model_class(self) -> type[SemanticSettingsEntity]:
        return SemanticSettingsEntity

    async def load(self, session: AsyncSession) -> SemanticSettingsEntity | None:
        """The stored settings, or ``None`` on an installation that has none.

        Carries the decrypted key, so this is the composition root's read and
        not a page's: it is what builds the embedder. Anything that ends up in
        front of a human asks :meth:`api_key_is_set` instead.
        """
        return await session.get(SemanticSettingsEntity, SEMANTIC_SETTINGS_ID)

    async def api_key_is_set(self, session: AsyncSession) -> bool:
        """Whether a key is stored — all a form is ever allowed to know.

        The ``IS NOT NULL`` is evaluated by the database, so what comes back
        over the wire is a boolean and the ciphertext is never fetched, never
        decrypted and never held in this process. A caller cannot put in a
        state var, a log line or a template what it was not given.
        """
        result = await session.execute(
            select(SemanticSettingsEntity.api_key.is_not(None)).where(
                SemanticSettingsEntity.id == SEMANTIC_SETTINGS_ID
            )
        )
        return bool(result.scalars().first())

    async def store(
        self,
        session: AsyncSession,
        *,
        provider: str | None,
        model: str | None,
        dimension: int | None,
        base_url: str | None,
        expected_updated: datetime | None = None,
    ) -> SemanticSettingsEntity:
        """Store everything except the key, creating the row on the first save.

        Every one of the four settings is required and every one accepts
        ``None``, because ``None`` is a value here and not an omission: it
        means "unset this and let the configuration file answer again". A form
        that leaves a field empty has to decide which of the two it meant, and
        defaulting the parameters would quietly decide it the other way round.

        ``expected_updated`` is the row's timestamp as the caller last read it,
        and is the whole of the concurrency control. Passing it turns the write
        into "store this *if* the row is still the one I read"; leaving it
        ``None`` means "I read no row", which is what a fresh installation and
        the reset control both honestly mean. It defaults to ``None`` so that
        the many callers with no baseline — migrations, tests, the reset — are
        not made to invent one, and the one caller that has a baseline is the
        one that needs it: see :class:`SettingsChangedElsewhere` for what the
        alternative costs.

        What it does **not** close: ``updated`` is ``func.now()``, which on
        SQLite is ``CURRENT_TIMESTAMP`` and counts in whole seconds, so two
        writes inside one second carry the same value and cannot be told apart
        here. That is the fast race — one person double-clicking — and it is
        closed where it happens, by the ``saving`` flag the three writing
        handlers check before they start. This closes the slow one: two people
        at two screens, minutes apart, which had no guard at all.
        """
        entity = await self._row(session)
        if expected_updated is not None and entity.updated != expected_updated:
            raise SettingsChangedElsewhere(
                "the embedder settings were changed by somebody else since this "
                "form was loaded"
            )
        entity.provider = provider
        entity.model = model
        entity.dimension = dimension
        entity.base_url = base_url
        await session.flush()
        # `updated` carries `onupdate=func.now()`, so the flush expires it and
        # the value the database actually wrote is only knowable by asking.
        # Refreshed here rather than left to the caller because the caller is
        # the one that has to hand it back as `expected_updated` next time, and
        # reading an expired attribute outside a greenlet raises MissingGreenlet
        # — a failure that would surface as a puzzle rather than as this rule.
        await session.refresh(entity, ["updated"])
        logger.info(
            "Embedder settings stored: provider=%s model=%s dimension=%s",
            provider,
            model,
            dimension,
        )
        return entity

    async def set_api_key(
        self, session: AsyncSession, api_key: str
    ) -> SemanticSettingsEntity:
        """Replace the stored key, creating the row if this is the first write.

        Encryption is the column's, not this method's: ``EncryptedString``
        reads the Fernet key off ``DatabaseConfig`` at write time, so a caller
        cannot accidentally store a key in the clear by using the wrong
        setter.

        A failing flush is re-raised stripped, and that is not tidiness.
        SQLAlchemy's ``StatementError`` quotes the statement *and its bind
        parameters*, and for this one statement the bind parameter is the
        plaintext key — measured against the agent sandbox, whose configured
        Fernet key was invalid:

            (builtins.ValueError) 'app_database_encryption_key' is not a valid
            32-byte url-safe base64-encoded Fernet key.
            [SQL: UPDATE semantic_settings SET api_key=?, ...]
            [parameters: [{'api_key': 'sk-sandbox', ...}]]

        So the one way this write can fail is also the one way the key reaches
        a log, and a caller doing the right thing — reporting the error — would
        be the thing that leaked it. ``from None`` is deliberate: chaining
        would put the original back in the traceback.
        """
        entity = await self._row(session)
        entity.api_key = api_key
        try:
            await session.flush()
        except StatementError as error:
            raise ApiKeyNotStored(_without_parameters(error)) from None
        logger.info("A new embedder API key was stored")
        return entity

    async def clear_api_key(self, session: AsyncSession) -> SemanticSettingsEntity:
        """Forget the stored key, leaving the rest of the settings alone.

        The other half of :meth:`store`'s refusal to touch it. Because an empty
        field on a form means "unchanged", removing a key has to be something a
        human asks for in as many words, and this is that request.
        """
        entity = await self._row(session)
        entity.api_key = None
        await session.flush()
        logger.info("The stored embedder API key was cleared")
        return entity

    async def _row(self, session: AsyncSession) -> SemanticSettingsEntity:
        """The settings row, created empty when nothing has been stored yet.

        The id is fixed rather than generated: the table's ``CHECK (id = 1)``
        is what makes "one embedder" the database's rule, and a row written
        with any other key is refused rather than becoming a second answer.
        """
        entity = await self.load(session)
        if entity is None:
            entity = SemanticSettingsEntity(id=SEMANTIC_SETTINGS_ID)
            session.add(entity)
        return entity
