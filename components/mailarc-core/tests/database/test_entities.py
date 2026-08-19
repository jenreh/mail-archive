"""Tests for the tables in :mod:`mailarc_core.database.entities`.

Everything under test here is DDL — unique constraints, defaults, the
encrypted column — so every test runs against a real SQLite file. A fake would
only prove that the fake works.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    """`EncryptedString` reads the key off the registry at write time.

    Registered for real rather than patched away: an entity whose secret is
    not actually encrypted would pass every other test in this file.
    """
    key = Fernet.generate_key().decode()
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(
        DatabaseConfig, DatabaseConfig.model_validate({"encryption_key": key})
    )
    yield key
    registry.restore(saved)


def account(**overrides) -> MailAccountEntity:
    values = {
        "provider": "gmail",
        "display_name": "Work",
        "email_address": "jens@example.com",
    }
    return MailAccountEntity(**(values | overrides))


async def stored_account(session: AsyncSession, **overrides) -> MailAccountEntity:
    entity = account(**overrides)
    session.add(entity)
    await session.flush()
    return entity


class TestMailAccount:
    async def test_rejects_the_same_address_twice_at_one_provider(
        self, session
    ) -> None:
        session.add(account())
        session.add(account(display_name="Work again"))

        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_allows_the_same_address_at_another_provider(self, session) -> None:
        session.add(account())
        session.add(account(provider="imap"))

        await session.flush()

    async def test_starts_enabled_and_idle(self, session) -> None:
        entity = await stored_account(session)

        assert entity.enabled is True
        assert AccountStatus(entity.status) is AccountStatus.IDLE

    async def test_a_status_round_trips_as_its_short_string(self, session) -> None:
        entity = await stored_account(session, status=AccountStatus.AUTH_ERROR)
        await session.refresh(entity)

        assert entity.status == "auth_error"
        assert AccountStatus(entity.status) is AccountStatus.AUTH_ERROR


class TestMailCredential:
    async def test_the_secret_is_unreadable_in_the_file(
        self, session, encryption_key
    ) -> None:
        entity = await stored_account(session)
        session.add(
            MailCredentialEntity(
                account_id=entity.id,
                kind=CredentialKind.OAUTH,
                secret='{"refresh_token": "1//top-secret"}',
            )
        )
        await session.flush()

        raw = await session.execute(text("SELECT secret FROM mail_credentials"))
        stored = raw.scalar_one()

        assert "top-secret" not in stored
        assert Fernet(encryption_key).decrypt(stored.encode()).decode() == (
            '{"refresh_token": "1//top-secret"}'
        )

    async def test_the_secret_comes_back_decrypted(
        self, session, encryption_key
    ) -> None:
        blob = '{"refresh_token": "1//top-secret"}'
        entity = await stored_account(session)
        credential = MailCredentialEntity(
            account_id=entity.id, kind=CredentialKind.OAUTH, secret=blob
        )
        session.add(credential)
        await session.flush()
        session.expunge(credential)

        reloaded = await session.get(MailCredentialEntity, credential.id)

        assert reloaded is not None
        assert reloaded.secret == blob

    async def test_rejects_a_second_credential_of_the_same_kind(
        self, session, encryption_key
    ) -> None:
        entity = await stored_account(session)
        for _ in range(2):
            session.add(
                MailCredentialEntity(
                    account_id=entity.id, kind=CredentialKind.OAUTH, secret="opaque"
                )
            )

        with pytest.raises(IntegrityError):
            await session.flush()


class TestSyncCheckpoint:
    async def test_rejects_a_second_checkpoint_for_one_scope(self, session) -> None:
        entity = await stored_account(session)
        for cursor in ("historyId=1", "historyId=2"):
            session.add(
                MailSyncCheckpointEntity(
                    account_id=entity.id, scope="INBOX", cursor=cursor
                )
            )

        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_allows_one_checkpoint_per_scope(self, session) -> None:
        entity = await stored_account(session)
        for scope in ("INBOX", "Sent"):
            session.add(
                MailSyncCheckpointEntity(account_id=entity.id, scope=scope, cursor=None)
            )

        await session.flush()

    async def test_starts_at_zero_messages_seen(self, session) -> None:
        entity = await stored_account(session)
        checkpoint = MailSyncCheckpointEntity(account_id=entity.id, scope="INBOX")
        session.add(checkpoint)

        await session.flush()

        assert checkpoint.messages_seen == 0
        assert checkpoint.cursor is None


class TestSyncJob:
    async def test_a_new_job_is_queued_uncancelled_and_at_zero(self, session) -> None:
        job = MailSyncJobEntity(kind=SyncJobKind.IMPORT)
        session.add(job)

        await session.flush()

        assert SyncJobState(job.state) is SyncJobState.QUEUED
        assert job.cancel_requested is False
        assert (job.messages_total, job.messages_done, job.messages_failed) == (0, 0, 0)

    async def test_kind_and_state_round_trip_as_short_strings(self, session) -> None:
        job = MailSyncJobEntity(
            kind=SyncJobKind.INCREMENTAL, state=SyncJobState.CANCELLED
        )
        session.add(job)
        await session.flush()
        await session.refresh(job)

        assert (job.kind, job.state) == ("incremental", "cancelled")

    async def test_a_whole_archive_job_needs_no_account(self, session) -> None:
        job = MailSyncJobEntity(kind=SyncJobKind.DERIVE)
        session.add(job)

        await session.flush()

        assert job.account_id is None


class TestArchivedMessage:
    async def test_rejects_the_same_provider_id_twice_for_one_account(
        self, session
    ) -> None:
        entity = await stored_account(session)
        for canonical in ("sha-a", "sha-b"):
            session.add(
                MailArchivedMessageEntity(
                    account_id=entity.id,
                    provider_message_id="18f0",
                    canonical_id=canonical,
                )
            )

        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_the_same_provider_id_may_reach_a_second_account(
        self, session
    ) -> None:
        first = await stored_account(session)
        second = await stored_account(session, email_address="other@example.com")
        for account_id in (first.id, second.id):
            session.add(
                MailArchivedMessageEntity(
                    account_id=account_id,
                    provider_message_id="18f0",
                    canonical_id="sha-a",
                )
            )

        await session.flush()

    async def test_stamps_itself_when_archived(self, session) -> None:
        entity = await stored_account(session)
        row = MailArchivedMessageEntity(
            account_id=entity.id, provider_message_id="18f0", canonical_id="sha-a"
        )
        session.add(row)

        await session.flush()

        assert row.archived_at is not None


class TestFailedMessage:
    async def test_keeps_the_reason_and_the_detail(self, session) -> None:
        entity = await stored_account(session)
        row = MailFailedMessageEntity(
            account_id=entity.id,
            provider_message_id="18f0",
            reason="permanent",
            detail="MIME payload is not decodable",
        )
        session.add(row)

        await session.flush()

        assert row.reason == "permanent"
        assert row.detail == "MIME payload is not decodable"
        assert row.occurred_at is not None

    async def test_a_detail_is_optional(self, session) -> None:
        entity = await stored_account(session)
        row = MailFailedMessageEntity(
            account_id=entity.id, provider_message_id="18f0", reason="permanent"
        )
        session.add(row)

        await session.flush()

        assert row.detail is None
