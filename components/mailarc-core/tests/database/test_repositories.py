"""Tests for the queries in :mod:`mailarc_core.database.repositories`.

Against a real SQLite file again: a repository that is not run against a
database proves nothing, and the batch lookup the engine leans on is exactly
the kind of statement a fake would get wrong.

Only the queries added on top of ``BaseRepository`` are tested here — appkit
owns the CRUD and tests it itself.
"""

import logging
import traceback
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mailarc_core.database.entities import (
    SEMANTIC_SETTINGS_ID,
    CredentialKind,
    MailAccountEntity,
    MailArchivedMessageEntity,
    MailCredentialEntity,
    MailSyncJobEntity,
    SyncJobKind,
    SyncJobState,
)
from mailarc_core.database.repositories import (
    ApiKeyNotStored,
    ArchivedMessageRepository,
    CredentialNotStored,
    FailedMessageRepository,
    MailAccountRepository,
    MailCredentialRepository,
    SemanticSettingsRepository,
    SettingsChangedElsewhere,
    SyncCheckpointRepository,
    SyncJobRepository,
    _without_parameters,
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

        assert isinstance(column.type, String)
        assert column.type.length is None, (
            "a canonical id has no length the sender owes us"
        )


class TestSemanticSettingsRepository:
    async def test_nothing_is_stored_on_a_fresh_installation(self, session) -> None:
        """The state the whole design has to keep working in: no row, and the
        composition root falls through to the configuration file."""
        assert await SemanticSettingsRepository().load(session) is None
        assert await SemanticSettingsRepository().api_key_is_set(session) is False

    async def test_the_first_store_creates_the_row(self, session) -> None:
        repository = SemanticSettingsRepository()

        stored = await repository.store(
            session,
            provider="openai",
            model="text-embedding-3-small",
            dimension=1536,
            base_url="",
        )

        assert stored.id == SEMANTIC_SETTINGS_ID
        assert stored.provider == "openai"
        assert stored.model == "text-embedding-3-small"
        assert stored.dimension == 1536
        assert stored.base_url == ""

    async def test_storing_twice_updates_the_one_row(self, session) -> None:
        repository = SemanticSettingsRepository()
        await repository.store(
            session,
            provider="ollama",
            model="nomic-embed-text",
            dimension=768,
            base_url="",
        )

        await repository.store(
            session, provider="openai", model=None, dimension=None, base_url=None
        )

        assert await repository.count(session) == 1
        stored = await repository.load(session)
        assert stored is not None
        assert stored.provider == "openai"

    async def test_a_second_editor_working_from_a_stale_reading_is_refused(
        self, session
    ) -> None:
        """Two administrators, one row, and no way to tell them apart until now.

        A saves nothing yet; B switches the archive to openai/1536, saves, and
        acts on the vector-index warning. A — still holding the reading from
        before B's save — changes only the model and presses Save, and every
        column goes back to A's captured tuple: provider to ollama, dimension
        to 768. B sees no error, and the archive is now embedding at a length
        the index B just provisioned does not match, which is precisely the
        silent failure §7.4 and ``index_advice`` exist to prevent — A's form
        compared against A's stale baseline and produced no warning at all.

        The write is refused rather than merged, because there is nothing to
        merge with: what A meant by "ollama" was a statement about a row that
        no longer exists.
        """
        repository = SemanticSettingsRepository()
        first = await repository.store(
            session,
            provider="ollama",
            model="nomic-embed-text",
            dimension=768,
            base_url="",
        )
        stale = first.updated
        await repository.store(
            session,
            provider="openai",
            model="text-embedding-3-large",
            dimension=1536,
            base_url="",
        )
        # The elapsed minute between two people at two screens, made explicit.
        # `func.now()` is `CURRENT_TIMESTAMP` on SQLite and counts in whole
        # seconds, so two writes inside one second carry the same timestamp and
        # this guard cannot see between them — which is stated in `store` and
        # is why it is the *slow* race this closes. The fast one, a double
        # click, is closed by the `saving` flag on the three handlers.
        moved_on = await repository.load(session)
        assert moved_on is not None
        moved_on.updated = stale + timedelta(minutes=1)
        await session.flush()

        with pytest.raises(SettingsChangedElsewhere):
            await repository.store(
                session,
                provider="ollama",
                model="nomic-embed-text",
                dimension=768,
                base_url="",
                expected_updated=stale,
            )

        stored = await repository.load(session)
        assert stored is not None
        assert stored.provider == "openai", "the second editor's save stands"
        assert stored.dimension == 1536

    async def test_the_editor_who_read_the_row_they_are_writing_over_is_allowed(
        self, session
    ) -> None:
        repository = SemanticSettingsRepository()
        first = await repository.store(
            session,
            provider="ollama",
            model="nomic-embed-text",
            dimension=768,
            base_url="",
        )

        await repository.store(
            session,
            provider="ollama",
            model="mxbai-embed-large",
            dimension=768,
            base_url="",
            expected_updated=first.updated,
        )

        stored = await repository.load(session)
        assert stored is not None
        assert stored.model == "mxbai-embed-large"

    async def test_the_first_ever_save_carries_no_baseline_and_is_allowed(
        self, session
    ) -> None:
        """``None`` means "I read no row", which is true of a fresh installation.

        Conflating it with "I read a row and it had no timestamp" would make
        the very first save impossible, so the check only runs when both a row
        and a baseline exist.
        """
        repository = SemanticSettingsRepository()

        stored = await repository.store(
            session,
            provider="ollama",
            model="",
            dimension=768,
            base_url="",
            expected_updated=None,
        )

        assert stored.provider == "ollama"

    async def test_a_baseline_against_a_row_that_was_deleted_is_refused(
        self, session
    ) -> None:
        """A reading of a row that is not there any more is still stale."""
        repository = SemanticSettingsRepository()

        with pytest.raises(SettingsChangedElsewhere):
            await repository.store(
                session,
                provider="ollama",
                model="",
                dimension=768,
                base_url="",
                expected_updated=datetime(2020, 1, 1, tzinfo=UTC),
            )

    async def test_none_unsets_a_value_rather_than_being_ignored(self, session) -> None:
        """``None`` is a value here: it means "let the configuration file
        answer this again". A store that skipped it would make a setting
        impossible to take back once given."""
        repository = SemanticSettingsRepository()
        await repository.store(
            session, provider="ollama", model="nomic", dimension=768, base_url="x"
        )

        await repository.store(
            session, provider=None, model=None, dimension=None, base_url=None
        )

        stored = await repository.load(session)
        assert stored is not None
        assert (stored.provider, stored.model, stored.dimension, stored.base_url) == (
            None,
            None,
            None,
            None,
        )

    async def test_storing_the_settings_never_touches_the_key(
        self, session, encryption_key
    ) -> None:
        """The security property, and the reason ``store`` has no ``api_key``
        parameter at all: "an empty field means leave the key alone" cannot be
        forgotten by a caller that was never given the chance to clear it."""
        repository = SemanticSettingsRepository()
        await repository.set_api_key(session, "sk-live-do-not-print")

        await repository.store(
            session, provider="openai", model=None, dimension=None, base_url=None
        )

        stored = await repository.load(session)
        assert stored is not None
        assert stored.api_key == "sk-live-do-not-print"

    async def test_the_key_can_be_stored_before_anything_else(
        self, session, encryption_key
    ) -> None:
        """Either write may be the first one; neither may require the other."""
        repository = SemanticSettingsRepository()

        stored = await repository.set_api_key(session, "sk-live-do-not-print")

        assert stored.id == SEMANTIC_SETTINGS_ID
        assert await repository.api_key_is_set(session) is True

    async def test_clearing_the_key_leaves_the_rest_alone(
        self, session, encryption_key
    ) -> None:
        """The explicit control a write-only secret needs. Without it the only
        way back from "a key is stored" would be to type another one."""
        repository = SemanticSettingsRepository()
        await repository.store(
            session, provider="openai", model="e5", dimension=1536, base_url=""
        )
        await repository.set_api_key(session, "sk-live-do-not-print")

        await repository.clear_api_key(session)

        stored = await repository.load(session)
        assert stored is not None
        assert stored.api_key is None
        assert stored.provider == "openai"
        assert stored.model == "e5"
        assert await repository.api_key_is_set(session) is False

    async def test_asking_whether_a_key_is_set_never_fetches_it(
        self, session, encryption_key
    ) -> None:
        """The answer a browser is allowed to have, proved by taking the
        cipher key away.

        ``IS NOT NULL`` is evaluated by the database, so the ciphertext is
        never fetched and never decrypted — and under a *different* Fernet key
        that is the difference between an answer and an exception. A caller
        cannot leak what it was never given.
        """
        repository = SemanticSettingsRepository()
        await repository.set_api_key(session, "sk-live-do-not-print")
        session.expunge_all()
        registry = service_registry()
        saved = registry.snapshot()
        registry.register_as(
            DatabaseConfig,
            DatabaseConfig.model_validate({"encryption_key": Fernet.generate_key()}),
        )
        try:
            assert await repository.api_key_is_set(session) is True
            with pytest.raises(InvalidToken):
                await repository.load(session)
        finally:
            registry.restore(saved)

    async def test_the_stored_key_comes_back_decrypted_for_the_composition_root(
        self, session, encryption_key
    ) -> None:
        """The one read that does carry it, because it is what builds the
        embedder. Everything in front of a human uses ``api_key_is_set``."""
        repository = SemanticSettingsRepository()
        await repository.set_api_key(session, "sk-live-do-not-print")
        session.expunge_all()

        stored = await repository.load(session)

        assert stored is not None
        assert stored.api_key == "sk-live-do-not-print"

    async def test_a_failing_write_does_not_quote_the_key(self, session) -> None:
        """The one way this write can fail is also the one way the key reaches
        a log: SQLAlchemy's ``StatementError`` prints the statement *and its
        bind parameters*, and here the bind parameter is the plaintext key.

        The failure is the one measured against the agent sandbox — a
        configured Fernet key that is not a valid one — and the assertion is
        that the reason survives while the key does not.

        Asserted against a *rendered traceback*, not only against the message.
        ``from None`` leaves ``__context__`` in place and sets
        ``__suppress_context__``; it is the renderer that then drops the
        original, so rendering is the only honest way to ask whether a caller
        who logs this would print the key.
        """
        registry = service_registry()
        saved = registry.snapshot()
        registry.register_as(
            DatabaseConfig,
            DatabaseConfig.model_validate({"encryption_key": "not-a-fernet-key"}),
        )
        repository = SemanticSettingsRepository()
        # Through a variable, the way a form would pass it: a traceback renders
        # each frame's source line, so a literal here would put the key in the
        # rendering by way of this test rather than by way of the code.
        secret = "sk-live-do-not-print"  # noqa: S105 - not a real key
        try:
            with pytest.raises(ApiKeyNotStored) as raised:
                await repository.set_api_key(session, secret)
        finally:
            registry.restore(saved)

        rendered = "".join(traceback.format_exception(raised.value))
        assert "sk-live" not in rendered
        assert "Fernet" in rendered
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__

    def test_a_statement_error_without_a_cause_still_says_something(self) -> None:
        """``StatementError.orig`` is optional in SQLAlchemy's own signature.

        The fallback is one sentence rather than an empty message, because a
        caller who logs this has nothing else to go on — and it still cannot
        say ``str(error)``, which is what would carry the key.
        """
        bare = StatementError("boom", "UPDATE semantic_settings", {}, None)

        assert _without_parameters(bare) == "the database refused the write"

    async def test_no_write_puts_the_key_in_the_log(
        self, session, encryption_key, caplog
    ) -> None:
        """Grepped by hand once; asserted here so it stays true. §7 forbids a
        secret in a parameterised log line, and this is the only class in the
        project that holds one in a local variable."""
        repository = SemanticSettingsRepository()

        with caplog.at_level(logging.DEBUG, logger="mailarc_core.database"):
            await repository.set_api_key(session, "sk-live-do-not-print")
            await repository.store(
                session,
                provider="openai",
                model="text-embedding-3-small",
                dimension=1536,
                base_url="",
            )
            await repository.clear_api_key(session)

        assert "sk-live" not in caplog.text
