"""Tests for the queries in :mod:`mailarc_core.database.repositories`.

Against a real SQLite file again: a repository that is not run against a
database proves nothing, and the batch lookup the engine leans on is exactly
the kind of statement a fake would get wrong.

Only the queries added on top of ``BaseRepository`` are tested here — appkit
owns the CRUD and tests it itself.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from cryptography.fernet import Fernet
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
    ArchivedMessageRepository,
    FailedMessageRepository,
    MailAccountRepository,
    MailCredentialRepository,
    SyncCheckpointRepository,
    SyncJobRepository,
)
from mailarc_core.mail.identity import canonical_id


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

        assert column.type.length is None, (
            "a canonical id has no length the sender owes us"
        )
