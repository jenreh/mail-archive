"""Tests for the queries in :mod:`mailarc_core.database.repositories`.

Against a real SQLite file again: a repository that is not run against a
database proves nothing, and the batch lookup the engine leans on is exactly
the kind of statement a fake would get wrong.

Only the queries added on top of ``BaseRepository`` are tested here — appkit
owns the CRUD and tests it itself.
"""

import traceback
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from cryptography.fernet import Fernet
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mailarc_core.database.entities import (
    CredentialKind,
    MailAccountEntity,
    MailArchivedMessageEntity,
    MailCredentialEntity,
    MailSyncJobEntity,
    SyncJobKind,
    SyncJobState,
)
from mailarc_core.database.repositories import (
    FAILURE_LIMIT,
    MAX_FAILURE_ROWS,
    ArchivedMessageRepository,
    CredentialNotStored,
    FailedMessageRepository,
    MailAccountRepository,
    MailCredentialRepository,
    SyncCheckpointRepository,
    SyncJobRepository,
    _capped,
)
from mailarc_core.mail.identity import canonical_id

TYPED_SECRET = '{"refresh_token": "typed"}'  # noqa: S105 - a fixture
ROTATED_SECRET = '{"refresh_token": "rotated"}'  # noqa: S105 - a fixture
OAUTH_BLOB = "oauth-blob"  # noqa: S105 - a fixture
PASSWORD_BLOB = "password-blob"  # noqa: S105 - a fixture
LEAKED_IF_QUOTED = '{"refresh_token": "1//top-secret"}'  # noqa: S105 - a fixture
"""What a Gmail credential holds, as the one thing a failing write must not say.

A module constant rather than a literal at the call: a traceback renders each
frame's source line, so writing it inline would put the token into the
rendering by way of the test rather than by way of the code under test.
"""


@pytest.fixture
async def session(tmp_path) -> AsyncIterator[AsyncSession]:
    """An open session on a fresh database with the tables created."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail-archive.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as open_session:
        yield open_session
    await engine.dispose()


@pytest.fixture
def encryption_key() -> Iterator[str]:
    """`EncryptedString` reads the key off the registry at write time."""
    key = Fernet.generate_key().decode()
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(
        DatabaseConfig, DatabaseConfig.model_validate({"encryption_key": key})
    )
    yield key
    registry.restore(saved)


async def stored_account(
    session: AsyncSession,
    email_address: str = "jens@example.com",
    provider: str = "gmail",
    enabled: bool = True,
) -> MailAccountEntity:
    entity = MailAccountEntity(
        provider=provider,
        display_name="Work",
        email_address=email_address,
        enabled=enabled,
    )
    session.add(entity)
    await session.flush()
    return entity


class TestMailAccountRepository:
    async def test_finds_an_account_by_its_natural_key(self, session) -> None:
        await stored_account(session)
        expected = await stored_account(session, email_address="jens@work.example")
        repository = MailAccountRepository()

        found = await repository.find_by_address(session, "gmail", "jens@work.example")

        assert found is not None
        assert found.id == expected.id

    async def test_the_same_address_at_another_provider_is_another_account(
        self, session
    ) -> None:
        await stored_account(session)
        repository = MailAccountRepository()

        assert (
            await repository.find_by_address(session, "imap", "jens@example.com")
        ) is None

    async def test_finds_only_the_enabled_accounts(self, session) -> None:
        enabled = await stored_account(session)
        await stored_account(session, email_address="old@example.com", enabled=False)
        repository = MailAccountRepository()

        found = await repository.find_enabled(session)

        assert [entity.id for entity in found] == [enabled.id]


class TestMailCredentialRepository:
    async def test_finds_the_credential_of_one_kind(
        self, session, encryption_key
    ) -> None:
        blob = '{"refresh_token": "1//top-secret"}'
        account = await stored_account(session)
        session.add(
            MailCredentialEntity(
                account_id=account.id, kind=CredentialKind.OAUTH, secret=blob
            )
        )
        await session.flush()
        repository = MailCredentialRepository()

        found = await repository.find_by_account(
            session, account.id, CredentialKind.OAUTH
        )

        assert found is not None
        assert found.secret == blob

    async def test_returns_nothing_for_a_kind_the_account_has_not_got(
        self, session, encryption_key
    ) -> None:
        account = await stored_account(session)
        session.add(
            MailCredentialEntity(
                account_id=account.id, kind=CredentialKind.OAUTH, secret="oauth-blob"
            )
        )
        await session.flush()
        repository = MailCredentialRepository()

        found = await repository.find_by_account(
            session, account.id, CredentialKind.PASSWORD
        )

        assert found is None

    async def test_stores_the_secret_on_the_first_write(
        self, session, encryption_key
    ) -> None:
        account = await stored_account(session)
        repository = MailCredentialRepository()

        stored = await repository.store_secret(
            session,
            account_id=account.id,
            kind=CredentialKind.OAUTH,
            secret=TYPED_SECRET,
        )

        assert stored.id is not None
        assert stored.secret == TYPED_SECRET

    async def test_the_second_write_replaces_the_row_of_that_kind(
        self, session, encryption_key
    ) -> None:
        """A rotated token supersedes the one it was issued against.

        The natural key is ``(account_id, kind)`` and the table constrains it,
        so a second write of the same kind has to be an update — inserting
        would be an ``IntegrityError``, and ``app/worker.py`` writes here on
        every run Google rotates a refresh token during.
        """
        account = await stored_account(session)
        repository = MailCredentialRepository()
        first = await repository.store_secret(
            session,
            account_id=account.id,
            kind=CredentialKind.OAUTH,
            secret=TYPED_SECRET,
        )

        second = await repository.store_secret(
            session,
            account_id=account.id,
            kind=CredentialKind.OAUTH,
            secret=ROTATED_SECRET,
        )

        assert second.id == first.id
        assert second.secret == ROTATED_SECRET

    async def test_the_other_kind_is_a_row_of_its_own(
        self, session, encryption_key
    ) -> None:
        """One secret per kind, not one per account: the form writes a
        ``password`` row and a consent round trip an ``oauth`` one."""
        account = await stored_account(session)
        repository = MailCredentialRepository()
        await repository.store_secret(
            session,
            account_id=account.id,
            kind=CredentialKind.OAUTH,
            secret=OAUTH_BLOB,
        )

        await repository.store_secret(
            session,
            account_id=account.id,
            kind=CredentialKind.PASSWORD,
            secret=PASSWORD_BLOB,
        )

        oauth = await repository.find_by_account(
            session, account.id, CredentialKind.OAUTH
        )
        password = await repository.find_by_account(
            session, account.id, CredentialKind.PASSWORD
        )
        assert oauth is not None
        assert oauth.secret == OAUTH_BLOB
        assert password is not None
        assert password.secret == PASSWORD_BLOB

    async def test_a_failing_write_does_not_quote_the_secret(self, session) -> None:
        """The hole :meth:`SemanticSettingsRepository.set_api_key` closed, on
        the column that carries a Gmail refresh token.

        ``StatementError`` prints the statement *and its bind parameters*, and
        on a write into an ``EncryptedString`` column the bind parameter is the
        plaintext secret — encrypting it is that column's bind processing,
        which is precisely what fails when the configured Fernet key is not a
        valid one. So the one way this write can fail is also the one way a
        refresh token reaches the log, and the code doing the right thing —
        the broad ``logger.exception`` handlers in ``accounts/state.py`` and in
        ``app/worker.py``'s ``_keep_refreshed_secret`` — would be the thing
        that leaked it.

        Asserted against a *rendered traceback*, not only against the message.
        ``from None`` leaves ``__context__`` in place and sets
        ``__suppress_context__``; it is the renderer that then drops the
        original, so rendering is the only honest way to ask whether a caller
        who logs this would print the token. See :data:`LEAKED_IF_QUOTED` for
        why the token is a constant and not a literal at the call.
        """
        account = await stored_account(session)
        registry = service_registry()
        saved = registry.snapshot()
        registry.register_as(
            DatabaseConfig,
            DatabaseConfig.model_validate({"encryption_key": "not-a-fernet-key"}),
        )
        repository = MailCredentialRepository()
        try:
            with pytest.raises(CredentialNotStored) as raised:
                await repository.store_secret(
                    session,
                    account_id=account.id,
                    kind=CredentialKind.OAUTH,
                    secret=LEAKED_IF_QUOTED,
                )
        finally:
            registry.restore(saved)

        rendered = "".join(traceback.format_exception(raised.value))
        assert "1//top-secret" not in rendered
        assert "Fernet" in rendered
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__


class TestSyncCheckpointRepository:
    async def test_creates_the_checkpoint_on_the_first_run_of_a_scope(
        self, session
    ) -> None:
        account = await stored_account(session)
        repository = SyncCheckpointRepository()

        checkpoint = await repository.upsert_cursor(
            session, account.id, "INBOX", "historyId=1", 200
        )

        assert checkpoint.cursor == "historyId=1"
        assert checkpoint.messages_seen == 200
        assert checkpoint.last_run_at is not None

    async def test_advances_the_same_row_on_the_next_run(self, session) -> None:
        account = await stored_account(session)
        repository = SyncCheckpointRepository()

        first = await repository.upsert_cursor(
            session, account.id, "INBOX", "historyId=1", 200
        )
        second = await repository.upsert_cursor(
            session, account.id, "INBOX", "historyId=2", 400
        )

        assert second.id == first.id
        assert second.cursor == "historyId=2"
        assert second.messages_seen == 400
        assert await repository.count(session) == 1

    async def test_keeps_a_cursor_per_scope(self, session) -> None:
        account = await stored_account(session)
        repository = SyncCheckpointRepository()
        await repository.upsert_cursor(session, account.id, "INBOX", "a", 1)
        await repository.upsert_cursor(session, account.id, "Sent", "b", 2)

        found = await repository.find_by_account_and_scope(session, account.id, "Sent")

        assert found is not None
        assert found.cursor == "b"

    async def test_finds_nothing_before_the_first_run(self, session) -> None:
        account = await stored_account(session)
        repository = SyncCheckpointRepository()

        found = await repository.find_by_account_and_scope(session, account.id, "INBOX")

        assert found is None


class TestSyncJobRepository:
    async def test_finds_the_queued_jobs_oldest_first(self, session) -> None:
        account = await stored_account(session)
        jobs = [
            MailSyncJobEntity(kind=SyncJobKind.IMPORT, account_id=account.id),
            MailSyncJobEntity(
                kind=SyncJobKind.IMPORT,
                account_id=account.id,
                state=SyncJobState.RUNNING,
            ),
            MailSyncJobEntity(kind=SyncJobKind.DERIVE),
        ]
        session.add_all(jobs)
        await session.flush()
        repository = SyncJobRepository()

        found = await repository.find_queued(session)

        assert [job.id for job in found] == [jobs[0].id, jobs[2].id]

    async def test_finds_the_jobs_of_one_state(self, session) -> None:
        session.add_all(
            [
                MailSyncJobEntity(kind=SyncJobKind.EMBED, state=SyncJobState.FAILED),
                MailSyncJobEntity(kind=SyncJobKind.EMBED),
            ]
        )
        await session.flush()
        repository = SyncJobRepository()

        found = await repository.find_by_state(session, SyncJobState.FAILED)

        assert [job.kind for job in found] == ["embed"]

    async def test_counts_the_jobs_of_every_state_in_one_statement(
        self, session
    ) -> None:
        """The queue badge, without loading the queue to measure it."""
        session.add_all(
            [
                MailSyncJobEntity(kind=SyncJobKind.IMPORT),
                MailSyncJobEntity(kind=SyncJobKind.DERIVE),
                MailSyncJobEntity(kind=SyncJobKind.EMBED, state=SyncJobState.RUNNING),
                MailSyncJobEntity(kind=SyncJobKind.EMBED, state=SyncJobState.FAILED),
                MailSyncJobEntity(kind=SyncJobKind.EMBED, state=SyncJobState.FAILED),
            ]
        )
        await session.flush()
        repository = SyncJobRepository()

        counted = await repository.count_by_state(session)

        assert counted == {"queued": 2, "running": 1, "failed": 2}

    async def test_a_state_with_no_jobs_is_absent_rather_than_nought(
        self, session
    ) -> None:
        """``GROUP BY`` cannot invent a row; a caller reads it with ``.get``."""
        session.add(MailSyncJobEntity(kind=SyncJobKind.IMPORT))
        await session.flush()
        repository = SyncJobRepository()

        counted = await repository.count_by_state(session)

        assert counted == {"queued": 1}
        assert counted.get(SyncJobState.SUCCEEDED, 0) == 0

    async def test_an_empty_queue_counts_nothing(self, session) -> None:
        assert await SyncJobRepository().count_by_state(session) == {}

    async def test_lists_the_newest_failed_jobs_first(self, session) -> None:
        """What the notification panel reads, and why it is not ``find_by_state``.

        ``find_by_state`` has no ``LIMIT`` and orders by id ascending, so a
        panel that wanted eight lines out of it read every failed job the
        archive ever had — oldest first, then threw all but the newest away.
        """
        session.add_all(
            [
                MailSyncJobEntity(
                    kind=SyncJobKind.EMBED,
                    state=SyncJobState.FAILED,
                    error="old",
                    finished_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
                ),
                MailSyncJobEntity(
                    kind=SyncJobKind.DERIVE,
                    state=SyncJobState.FAILED,
                    error="new",
                    finished_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
                ),
                MailSyncJobEntity(kind=SyncJobKind.IMPORT),
            ]
        )
        await session.flush()

        found = await SyncJobRepository().find_recent_failed(session, limit=10)

        assert [job.error for job in found] == ["new", "old"]

    async def test_the_limit_is_the_size_of_the_failure_page(self, session) -> None:
        session.add_all(
            [
                MailSyncJobEntity(
                    kind=SyncJobKind.EMBED,
                    state=SyncJobState.FAILED,
                    error=f"job-{index}",
                    finished_at=datetime(2026, 8, 20 + index, 9, 0, tzinfo=UTC),
                )
                for index in range(4)
            ]
        )
        await session.flush()

        found = await SyncJobRepository().find_recent_failed(session, limit=2)

        assert [job.error for job in found] == ["job-3", "job-2"]

    async def test_a_failure_with_no_finish_time_is_still_reported(
        self, session
    ) -> None:
        """A job killed mid-write never wrote ``finished_at``, and a panel that
        dropped it would hide the worst failure there is."""
        session.add_all(
            [
                MailSyncJobEntity(
                    kind=SyncJobKind.EMBED,
                    state=SyncJobState.FAILED,
                    error="stamped",
                    finished_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
                ),
                MailSyncJobEntity(
                    kind=SyncJobKind.EMBED, state=SyncJobState.FAILED, error="unstamped"
                ),
            ]
        )
        await session.flush()

        found = await SyncJobRepository().find_recent_failed(session, limit=10)

        assert {job.error for job in found} == {"stamped", "unstamped"}

    async def test_a_stray_nought_still_asks_for_one_failed_job(self, session) -> None:
        session.add(
            MailSyncJobEntity(kind=SyncJobKind.EMBED, state=SyncJobState.FAILED)
        )
        await session.flush()

        found = await SyncJobRepository().find_recent_failed(session, limit=0)

        assert len(found) == 1

    async def test_a_healthy_queue_reports_no_failures(self, session) -> None:
        session.add(MailSyncJobEntity(kind=SyncJobKind.IMPORT))
        await session.flush()

        assert await SyncJobRepository().find_recent_failed(session, limit=5) == []

    async def test_finds_what_runs_against_one_mailbox(self, session) -> None:
        mine = await stored_account(session)
        other = await stored_account(session, email_address="other@example.com")
        running = MailSyncJobEntity(
            kind=SyncJobKind.IMPORT, account_id=mine.id, state=SyncJobState.RUNNING
        )
        session.add_all(
            [
                running,
                MailSyncJobEntity(kind=SyncJobKind.IMPORT, account_id=mine.id),
                MailSyncJobEntity(
                    kind=SyncJobKind.IMPORT,
                    account_id=other.id,
                    state=SyncJobState.RUNNING,
                ),
            ]
        )
        await session.flush()
        repository = SyncJobRepository()

        found = await repository.find_running_for_account(session, mine.id)

        assert [job.id for job in found] == [running.id]


class TestArchivedMessageRepository:
    async def test_returns_exactly_the_ids_it_already_knows(self, session) -> None:
        mine = await stored_account(session)
        other = await stored_account(session, email_address="other@example.com")
        repository = ArchivedMessageRepository()
        await repository.record_many(session, mine.id, {"a": "sha-a", "b": "sha-b"})
        await repository.record_many(session, other.id, {"c": "sha-c"})

        known = await repository.find_known_provider_ids(
            session, mine.id, ["a", "b", "c", "d"]
        )

        assert known == {"a", "b"}

    async def test_an_empty_batch_asks_nothing(self, session) -> None:
        account = await stored_account(session)
        repository = ArchivedMessageRepository()

        assert (
            await repository.find_known_provider_ids(session, account.id, []) == set()
        )

    async def test_records_a_row_per_message_under_one_timestamp(self, session) -> None:
        account = await stored_account(session)
        repository = ArchivedMessageRepository()
        archived_at = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)

        rows = await repository.record_many(
            session, account.id, {"a": "sha-a", "b": "sha-b"}, archived_at
        )

        assert await repository.count(session) == 2
        assert {row.canonical_id for row in rows} == {"sha-a", "sha-b"}
        assert {row.archived_at.replace(tzinfo=UTC) for row in rows} == {archived_at}

    async def test_the_canonical_id_stays_with_its_provider_id(self, session) -> None:
        account = await stored_account(session)
        repository = ArchivedMessageRepository()
        await repository.record_many(session, account.id, {"18f0": "sha-a"})

        rows = await repository.find_all(session)

        assert [(row.provider_message_id, row.canonical_id) for row in rows] == [
            ("18f0", "sha-a")
        ]


class TestFailedMessageRepository:
    async def test_leaves_a_row_for_a_skipped_message(self, session) -> None:
        account = await stored_account(session)
        repository = FailedMessageRepository()

        row = await repository.record(
            session, account.id, "18f0", "permanent", "MIME payload is not decodable"
        )

        assert row.id is not None
        assert row.reason == "permanent"
        assert row.detail == "MIME payload is not decodable"
        assert row.occurred_at is not None

    async def test_the_detail_may_be_left_out(self, session) -> None:
        account = await stored_account(session)
        repository = FailedMessageRepository()

        row = await repository.record(session, account.id, "18f0", "permanent")

        assert row.detail is None

    async def test_lists_the_newest_failures_first(self, session) -> None:
        account = await stored_account(session)
        repository = FailedMessageRepository()
        for index in range(3):
            row = await repository.record(
                session, account.id, f"m-{index}", "permanent"
            )
            row.occurred_at = datetime(2026, 8, 20 + index, 9, 0, tzinfo=UTC)
        await session.flush()

        found = await repository.find_recent(session, limit=10)

        assert [row.provider_message_id for row in found] == ["m-2", "m-1", "m-0"]

    async def test_the_limit_is_the_size_of_the_page(self, session) -> None:
        account = await stored_account(session)
        repository = FailedMessageRepository()
        for index in range(4):
            row = await repository.record(
                session, account.id, f"m-{index}", "permanent"
            )
            row.occurred_at = datetime(2026, 8, 20 + index, 9, 0, tzinfo=UTC)
        await session.flush()

        found = await repository.find_recent(session, limit=2)

        assert [row.provider_message_id for row in found] == ["m-3", "m-2"]

    async def test_a_healthy_archive_lists_nothing(self, session) -> None:
        assert await FailedMessageRepository().find_recent(session, limit=5) == []

    async def test_a_stray_nought_is_not_read_as_an_empty_archive(
        self, session
    ) -> None:
        """``LIMIT 0`` is legal SQL that returns nothing — the same trap
        `mailarc_analytics`' `_limit` closes. One row is the smallest honest
        answer."""
        account = await stored_account(session)
        await FailedMessageRepository().record(session, account.id, "18f0", "permanent")

        found = await FailedMessageRepository().find_recent(session, limit=0)

        assert len(found) == 1


class TestTheFailurePageCap:
    """`_capped` is the bottom and the top of what `find_recent` will fetch.

    Tested on the helper rather than through a table of half a million rows:
    the ceiling is the case that cannot be staged, and it is the one that
    matters — a panel is a panel, and `find_recent(limit=10_000_000)` would
    otherwise pull the whole ledger into a list to render twenty lines of it.
    """

    def test_an_ordinary_page_passes_through_untouched(self) -> None:
        assert _capped(FAILURE_LIMIT) == FAILURE_LIMIT

    def test_a_stray_nought_becomes_one_row(self) -> None:
        assert _capped(0) == 1

    def test_a_negative_becomes_one_row(self) -> None:
        assert _capped(-5) == 1

    def test_a_greedy_caller_is_clamped_at_the_ceiling(self) -> None:
        assert _capped(10_000_000) == MAX_FAILURE_ROWS


class TestTheCanonicalIdColumn:
    """It has to hold whatever `mailarc_core.mail.identity` produces.

    SQLite ignores a `VARCHAR(n)` length, so a column too narrow for a real id
    passes every test here and only fails on PostgreSQL — in production, on the
    first message that arrived without a `Message-ID`.
    """

    async def test_the_sha256_fallback_id_fits(self, session) -> None:
        account = await stored_account(session)
        repository = ArchivedMessageRepository()
        fallback = canonical_id(
            rfc_message_id=None,
            sent_at=None,
            sender=None,
            subject="Angebot",
            body_bytes=b"Hallo.",
        )
        assert len(fallback) == 71, "sha256: plus 64 hex — the shortest it ever is"

        await repository.record_many(session, account.id, {"p-1": fallback})

        rows = await repository.find_all(session)
        assert [row.canonical_id for row in rows] == [fallback]

    async def test_a_long_real_message_id_fits(self, session) -> None:
        """No part of an RFC 5322 Message-ID is ours to cap."""
        account = await stored_account(session)
        repository = ArchivedMessageRepository()
        long_id = f"{'CAJ7XQb9k2vZ' * 20}@mail.example.com"

        await repository.record_many(session, account.id, {"p-1": long_id})

        assert await repository.find_known_provider_ids(
            session, account.id, ["p-1"]
        ) == {"p-1"}
        rows = await repository.find_all(session)
        assert rows[0].canonical_id == long_id

    def test_the_column_is_not_a_bounded_string(self) -> None:
        """Stated once here so a later `String(n)` has to argue with a test."""
        column = MailArchivedMessageEntity.__table__.c.canonical_id

        assert isinstance(column.type, String)
        assert column.type.length is None, (
            "a canonical id has no length the sender owes us"
        )
