"""The one embedder settings row, and the write API that cannot leak its key.

Split out of ``test_repositories.py``, which was over the thousand-line limit.
The seam is the subject rather than the file size: everything here is about a
**write-only secret** — that ``store`` has no ``api_key`` parameter at all, that
a stale editor is refused rather than merged, and that a failing write never
renders the key into its own traceback. That last claim is why the file reaches
for ``traceback`` and a deliberately narrowed column, which nothing else in the
sibling file needs.

Against a real SQLite file, like its sibling: an ``EncryptedString`` column that
is not written through a driver proves nothing about what a driver would say
when the write fails.
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
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mailarc_core.database.entities import SEMANTIC_SETTINGS_ID
from mailarc_core.database.repositories import (
    ApiKeyNotStored,
    SemanticSettingsRepository,
    SettingsChangedElsewhere,
    _without_parameters,
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
    """`EncryptedString` reads the key off the registry at write time."""
    key = Fernet.generate_key().decode()
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(
        DatabaseConfig, DatabaseConfig.model_validate({"encryption_key": key})
    )
    yield key
    registry.restore(saved)


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
