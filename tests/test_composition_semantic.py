"""The composition root's embedder settings: stored over file, and what breaks.

Its own module and not part of ``test_composition.py`` for one reason: that
file was 1210 lines with this in it, and §5 caps a file at 1000. The split is
along a seam rather than at a line number — everything here is about the one
question of what :func:`app.composition.semantic_config` answers once a human
has stored something, which is the only part of the composition root that reads
a database.

The precedence rule itself belongs to ``mailarc-analytics`` and is proved there,
in ``test_semantic_config.py``, beside the defaults it overrides. What is proved
here is the half only the composition root can answer for.

The fixtures are this module's own rather than imported from its neighbour. A
pytest fixture is resolved by where it is defined, so sharing them would mean a
``tests/conftest.py`` whose autouse cache-clearing then applied to every module
under ``tests/`` — a wider change than the split is worth.
"""

import asyncio
import contextlib
import functools
import logging
from collections.abc import AsyncIterator, Iterator

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import composition
from mailarc_analytics.semantic import (
    SemanticConfig,
    SemanticControl,
    SemanticOverrides,
    SemanticProvider,
    SemanticSearch,
)
from mailarc_core import GraphConfig, GraphServerMode
from mailarc_sync.jobs import SessionFactory
from mailarc_ui.search.reads import semantic_search as search_the_ui_sees


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    """What the merge memoises, cleared before and after each test.

    ``_semantic_override`` is module state rather than a cache and is reset the
    same way: a test that adopted stored settings would otherwise decide what
    every later test's ``semantic_config()`` answers.
    """
    _reset()
    yield
    _reset()


def _reset() -> None:
    composition._semantic_override = None
    composition.semantic_embedder.cache_clear()
    composition.semantic_search.cache_clear()


@pytest.fixture
def _published_registry() -> Iterator[None]:
    """Publishing writes into the process-wide registry; put it back after."""
    registry = service_registry()
    saved = registry.snapshot()
    yield
    registry.restore(saved)


def _use_config(monkeypatch, mode: GraphServerMode) -> GraphConfig:
    """Point the root at a graph nothing has to be listening on.

    Only the search is built against it here, and building one opens no
    connection — the session factory is a ``partial`` it calls per query.
    """
    config = GraphConfig(mode=mode, host="127.0.0.1", port=6379)
    monkeypatch.setattr(composition, "graph_config", lambda: config)
    return config


@pytest.fixture
async def settings_store(tmp_path) -> AsyncIterator[SessionFactory]:
    """A session factory on a database this test owns, with the tables created.

    Its own file rather than the configured one: ``load_semantic_config`` takes
    the factory as a parameter precisely so a test can point it somewhere, and
    the suite's configured database is an in-memory URL shared with everything
    else in the run.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def open_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session
            await session.commit()

    yield open_session
    await engine.dispose()


@pytest.fixture
def cipher_key() -> Iterator[str]:
    """A real Fernet key, because the API key column really is encrypted.

    ``configuration/config.test.yaml`` carries a placeholder Fernet refuses,
    which is correct — the suite has no business holding a usable key — so a
    test that writes a secret registers one of its own.
    """
    key = Fernet.generate_key().decode()
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(
        DatabaseConfig, DatabaseConfig.model_validate({"encryption_key": key})
    )
    yield key
    registry.restore(saved)


@pytest.fixture
def file_semantic() -> Iterator[SemanticConfig]:
    """What ``configuration/config.yaml`` and the environment resolved to.

    Every overridable field stated, so the assertions below are about
    precedence rather than about whatever the machine running them is set to.
    """
    config = SemanticConfig(
        provider=SemanticProvider.OLLAMA,
        model="from-the-file",
        dimension=768,
        base_url="http://file.invalid",
        api_key=None,
    )
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(SemanticConfig, config)
    yield config
    registry.restore(saved)


async def store_settings(
    session_factory: SessionFactory,
    *,
    provider: str | None = None,
    model: str | None = None,
    dimension: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> None:
    """Write a settings row the way a form will: the key last, and separately."""
    async with session_factory() as session:
        await composition._SEMANTIC_SETTINGS.store(
            session,
            provider=provider,
            model=model,
            dimension=dimension,
            base_url=base_url,
        )
        if api_key is not None:
            await composition._SEMANTIC_SETTINGS.set_api_key(session, api_key)


class ClosingEmbedder:
    """An embedder that only records having been closed.

    Enough of :class:`~mailarc_analytics.semantic.ports.EmbedderPort` for the
    one question asked below — whether replacing the configuration releases the
    ``httpx`` pool the old adapter holds — and none of the HTTP, because no
    test in this repository may reach Ollama or OpenAI.
    """

    def __init__(self, config: SemanticConfig) -> None:
        self.model = config.model
        self.dimension = config.dimension
        self.closed = False

    async def embed(self, texts: object, *, purpose: object = None) -> None:
        raise AssertionError("no test may embed anything")

    async def aclose(self) -> None:
        self.closed = True


class TestTheStoredEmbedderSettings:
    """Stored over file, unset falls through, and a fresh install is untouched.

    The precedence rule itself is proved in ``mailarc-analytics``, next to the
    defaults it overrides. What is proved here is the half only the composition
    root can answer for: that reading the row changes what
    :func:`semantic_config` returns, that the objects built from the previous
    answer are dropped and closed, and that an installation with nothing stored
    ends up exactly where it started.
    """

    def test_the_configuration_is_the_file_until_it_is_loaded(
        self, file_semantic
    ) -> None:
        """Not a fallback — the guarantee. A worker, a migration or a CLI that
        never opens the database sees what it saw before any of this existed."""
        assert composition.semantic_config() is file_semantic

    async def test_a_fresh_installation_still_resolves_to_no_embedder(
        self, settings_store, monkeypatch
    ) -> None:
        """§7.4's default, run through the whole merge. Nothing is stored, so
        the registered configuration answers — and on a default installation
        that is ``provider: none``: full text keeps working and semantic search
        is the one thing that says it is off."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)
        registry = service_registry()
        saved = registry.snapshot()
        registry.register_as(SemanticConfig, SemanticConfig())
        try:
            merged = await composition.load_semantic_config(settings_store)

            assert merged.provider is SemanticProvider.NONE
            assert composition.semantic_embedder() is None
            assert not composition.semantic_search().available
        finally:
            registry.restore(saved)

    async def test_nothing_stored_is_the_configured_object_itself(
        self, settings_store, file_semantic
    ) -> None:
        """Identity, so the caller can see there is nothing to rebuild."""
        assert await composition.load_semantic_config(settings_store) is file_semantic

    async def test_nothing_stored_rebuilds_nothing(
        self, settings_store, file_semantic, monkeypatch
    ) -> None:
        """The strongest form of "behaves exactly as today": with no row the
        load clears no cache, closes no client and re-registers nothing, so the
        objects a fresh installation is already holding are the objects it goes
        on holding."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)
        monkeypatch.setattr(composition, "build_embedder", ClosingEmbedder)
        before = composition.semantic_search()
        embedder = composition.semantic_embedder()
        assert isinstance(embedder, ClosingEmbedder)

        await composition.load_semantic_config(settings_store)

        assert composition.semantic_search() is before
        assert composition.semantic_embedder() is embedder
        assert not embedder.closed

    async def test_a_stored_value_beats_the_file(
        self, settings_store, file_semantic
    ) -> None:
        await store_settings(settings_store, provider="openai")

        merged = await composition.load_semantic_config(settings_store)

        assert merged.provider is SemanticProvider.OPENAI
        assert composition.semantic_config().provider is SemanticProvider.OPENAI

    async def test_an_unset_stored_value_falls_through_to_the_file(
        self, settings_store, file_semantic
    ) -> None:
        """Changing the provider has not thereby cleared the model.

        The worked example: the file says ``ollama`` / ``from-the-file`` / 768
        / ``http://file.invalid``, the row says ``openai`` and nothing else,
        and what comes out is ``openai`` with the other three untouched.
        """
        await store_settings(settings_store, provider="openai")

        merged = await composition.load_semantic_config(settings_store)

        assert merged.provider is SemanticProvider.OPENAI
        assert merged.model == "from-the-file"
        assert merged.dimension == 768
        assert merged.base_url == "http://file.invalid"

    async def test_a_stored_dimension_is_carried_through(
        self, settings_store, file_semantic
    ) -> None:
        """The setting §7.4 calls the one that is not free to change. It has to
        reach the embedder, because ``Message.embedding_model`` and the live
        index are what make a mismatch detectable rather than silent."""
        await store_settings(settings_store, provider="openai", dimension=1536)

        merged = await composition.load_semantic_config(settings_store)

        assert merged.dimension == 1536

    async def test_the_stored_key_reaches_the_configuration(
        self, settings_store, file_semantic, cipher_key
    ) -> None:
        """Encrypted at rest, decrypted once, on its way to the adapter that
        spends it — and nowhere else."""
        await store_settings(
            settings_store, provider="openai", api_key="sk-live-do-not-print"
        )

        merged = await composition.load_semantic_config(settings_store)

        assert merged.api_key is not None
        assert merged.api_key.get_secret_value() == "sk-live-do-not-print"

    async def test_the_merged_configuration_does_not_print_the_key(
        self, settings_store, file_semantic, cipher_key
    ) -> None:
        await store_settings(
            settings_store, provider="openai", api_key="sk-live-do-not-print"
        )

        merged = await composition.load_semantic_config(settings_store)

        assert "sk-live" not in repr(merged)

    async def test_adopting_a_change_closes_the_embedder_it_replaces(
        self, settings_store, file_semantic, monkeypatch
    ) -> None:
        """An adapter holds an ``httpx`` pool. A cache cleared without an
        ``aclose`` leaks a connection every time somebody saves the form."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)
        monkeypatch.setattr(composition, "build_embedder", ClosingEmbedder)
        stale = composition.semantic_embedder()
        assert isinstance(stale, ClosingEmbedder)
        await store_settings(settings_store, provider="openai")

        await composition.load_semantic_config(settings_store)

        assert stale.closed
        assert composition.semantic_embedder() is not stale

    async def test_a_change_does_not_build_an_embedder_only_to_close_it(
        self, settings_store, file_semantic, monkeypatch
    ) -> None:
        """Nothing had asked for one yet, so nothing is constructed here — the
        adopt reads the cache rather than the factory."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)
        built: list[SemanticConfig] = []

        def recording(config: SemanticConfig) -> ClosingEmbedder:
            built.append(config)
            return ClosingEmbedder(config)

        monkeypatch.setattr(composition, "build_embedder", recording)
        await store_settings(settings_store, provider="openai")

        await composition.load_semantic_config(settings_store)

        assert built == []

    @pytest.mark.usefixtures("_published_registry")
    async def test_the_search_the_ui_reads_is_republished(
        self, settings_store, file_semantic, monkeypatch
    ) -> None:
        """``mailarc-ui`` may not import ``app``, so it holds whatever the
        registry holds. A stale object there would stay stale for the life of
        the process."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)
        before = composition.publish_semantic_search()
        await store_settings(settings_store, provider="openai")

        await composition.load_semantic_config(settings_store)

        assert search_the_ui_sees() is not before
        assert search_the_ui_sees() is composition.semantic_search()

    @pytest.mark.usefixtures("_published_registry")
    async def test_an_unpublished_search_stays_unpublished(
        self, settings_store, file_semantic, monkeypatch
    ) -> None:
        """The worker adopts the same settings and has no page to serve. A load
        must not decide on its behalf that a search belongs in the registry."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)
        registry = service_registry()
        if registry.has(SemanticSearch):
            registry.unregister(SemanticSearch)
        await store_settings(settings_store, provider="openai")

        await composition.load_semantic_config(settings_store)

        assert not registry.has(SemanticSearch)

    async def test_loading_twice_changes_nothing_the_second_time(
        self, settings_store, file_semantic, monkeypatch
    ) -> None:
        """Idempotent, so the hook and a later save can both call it."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)
        await store_settings(settings_store, provider="openai")
        await composition.load_semantic_config(settings_store)
        first = composition.semantic_search()

        await composition.load_semantic_config(settings_store)

        assert composition.semantic_search() is first


class TestAStoredRowThatMakesNoSense:
    """A hand-edited database, or a downgrade that left a newer value behind.

    The archive keeps the embedder its configuration file describes rather than
    refusing to start, and the log names the fields it refused without quoting
    any of them — one of the five is an API key, and pydantic's own message
    carries the input of every field it rejected.
    """

    async def test_a_provider_no_release_ever_wrote_is_ignored(
        self, settings_store, file_semantic
    ) -> None:
        await store_settings(settings_store, provider="gemini")

        merged = await composition.load_semantic_config(settings_store)

        assert merged is file_semantic

    async def test_a_dimension_that_cannot_produce_a_vector_is_ignored(
        self, settings_store, file_semantic
    ) -> None:
        """Zero is not a smaller index — it is an embedder that can never write
        a vector the graph will accept, and FalkorDB stores a wrong length
        without complaint and simply does not index it.

        What it costs is the dimension and nothing else: the file's length
        answers again, and the rest of the row stands. See
        ``test_one_bad_column_costs_that_column_and_not_the_row`` for why the
        whole row used to go instead.
        """
        await store_settings(settings_store, provider="openai", dimension=0)

        merged = await composition.load_semantic_config(settings_store)

        assert merged.dimension == file_semantic.dimension

    async def test_the_refused_fields_are_named_in_the_log(
        self, settings_store, file_semantic, caplog
    ) -> None:
        await store_settings(settings_store, provider="gemini", dimension=0)

        with caplog.at_level(logging.WARNING, logger="app.composition"):
            await composition.load_semantic_config(settings_store)

        assert "provider" in caplog.text
        assert "dimension" in caplog.text

    async def test_the_key_is_not_in_that_log_line(
        self, settings_store, file_semantic, cipher_key, caplog
    ) -> None:
        """Why only field names are logged: ``ValidationError`` quotes the
        input of everything it refused, so a future refusal of ``api_key``
        would put the key in the log of a process doing the right thing."""
        await store_settings(
            settings_store, provider="gemini", api_key="sk-live-do-not-print"
        )

        with caplog.at_level(logging.DEBUG):
            await composition.load_semantic_config(settings_store)

        assert "sk-live" not in caplog.text

    async def test_one_bad_column_costs_that_column_and_not_the_row(
        self, settings_store, file_semantic
    ) -> None:
        """The other four survive, because they are four separate decisions.

        A row of a good provider, a good model and a hand-edited ``dimension``
        of zero used to be discarded whole, so the archive silently ran the
        *file's* embedder — on a default installation ``provider: none`` — while
        the settings page went on redisplaying the stored provider. Nothing
        anywhere said which embedder was in force.
        """
        await store_settings(
            settings_store,
            provider="openai",
            model="text-embedding-3-large",
            dimension=0,
        )

        merged = await composition.load_semantic_config(settings_store)

        assert merged.provider is SemanticProvider.OPENAI
        assert merged.model == "text-embedding-3-large"
        assert merged.dimension == file_semantic.dimension

    async def test_what_survived_is_written_down(
        self, settings_store, file_semantic, caplog
    ) -> None:
        """ "Which embedder is this process running" has to be answerable later.

        A partial merge is the case where the answer is neither the file nor
        the row, so the adoption line carries all three identifying values.
        """
        await store_settings(settings_store, provider="openai", dimension=0)

        with caplog.at_level(logging.INFO, logger="app.composition"):
            await composition.load_semantic_config(settings_store)

        assert "openai" in caplog.text
        assert str(file_semantic.dimension) in caplog.text

    async def test_a_refusal_that_names_no_field_drops_the_row_rather_than_raising(
        self, settings_store, file_semantic, cipher_key, monkeypatch, caplog
    ) -> None:
        """The guard must not fail inside the ``except`` that exists to guard.

        A pydantic error whose ``loc`` is empty — what a model validator or a
        root-level type error produces — used to make ``one["loc"][0]`` raise
        ``IndexError`` out of the handler, chaining the ``ValidationError``
        into a traceback that the lifespan then rendered with
        ``logger.exception`` — ``input_value=`` of every refused field
        included, and one of the five is the key. Unreachable through
        ``SemanticOverrides`` as it stands today, which is exactly why it is
        pinned here rather than left to be discovered by the first validator
        somebody adds.
        """

        real = composition.SemanticOverrides

        def rootwise(**fields: object) -> SemanticOverrides:
            """A model validator's refusal: blamed on the row, not on a field."""
            if not fields:
                return real()
            raise ValidationError.from_exception_data(
                "SemanticOverrides",
                [
                    {
                        "type": "value_error",
                        "loc": (),
                        "input": "sk-live-do-not-print",
                        "ctx": {"error": ValueError("the whole row")},
                    }
                ],
            )

        await store_settings(
            settings_store, provider="openai", api_key="sk-live-do-not-print"
        )
        monkeypatch.setattr(composition, "SemanticOverrides", rootwise)

        with caplog.at_level(logging.DEBUG):
            merged = await composition.load_semantic_config(settings_store)

        assert merged is file_semantic
        assert "sk-live" not in caplog.text


class TestTheSemanticSettingsLifespan:
    """The hook ``app/app.py`` registers, and what it does when it cannot read.

    A failure is logged and swallowed, the same policy the graph server and the
    worker hooks get. There are two real ones — a database whose migration has
    not been applied, and a row somebody edited by hand — and neither is a
    reason for an archive to refuse to open.
    """

    async def test_it_adopts_what_is_stored(
        self, settings_store, file_semantic, monkeypatch
    ) -> None:
        await store_settings(settings_store, provider="openai")
        monkeypatch.setattr(
            composition,
            "load_semantic_config",
            functools.partial(composition.load_semantic_config, settings_store),
        )

        async with composition.semantic_settings_lifespan():
            assert composition.semantic_config().provider is SemanticProvider.OPENAI

    async def test_an_unmigrated_database_really_does_raise(
        self, tmp_path, file_semantic
    ) -> None:
        """So the swallow below is not guarding a case that cannot happen.

        ``semantic_settings`` arrives in a revision a fresh checkout has not
        applied yet, and this is what the read does then.
        """
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)

        @contextlib.asynccontextmanager
        async def open_session() -> AsyncIterator[AsyncSession]:
            async with factory() as session:
                yield session

        try:
            with pytest.raises(OperationalError, match="semantic_settings"):
                await composition.load_semantic_config(open_session)
        finally:
            await engine.dispose()

    async def test_a_read_that_fails_does_not_stop_the_application(
        self, file_semantic, caplog, monkeypatch
    ) -> None:
        async def refusing() -> SemanticConfig:
            raise OperationalError(
                "SELECT semantic_settings", {}, RuntimeError("no such table")
            )

        monkeypatch.setattr(composition, "load_semantic_config", refusing)

        with caplog.at_level(logging.ERROR, logger="app.composition"):
            async with composition.semantic_settings_lifespan():
                assert composition.semantic_config() is file_semantic

        assert "stored embedder settings" in caplog.text


class TestPublishingTheControlTheSettingsPageReads:
    """The seam the embedder form reaches the composition root through.

    ``mailarc-ui`` may not import ``app``, so the form asks the registry for a
    :class:`~mailarc_analytics.semantic.config.SemanticControl` and calls what
    it finds. What matters is that the entry carries the *functions* and not
    their results: the effective configuration changes on every save, and an
    entry holding the object it was handed at startup would go on reporting the
    embedder it replaced.
    """

    @pytest.mark.usefixtures("_published_registry")
    def test_it_leaves_the_two_verbs_in_the_registry(self) -> None:
        published = composition.publish_semantic_control()

        assert service_registry().get(SemanticControl) is published
        assert published.reload is composition.load_semantic_config

    @pytest.mark.usefixtures("_published_registry")
    def test_saying_it_twice_is_a_no_op(self) -> None:
        """The application is reloadable; a second call must not overwrite."""
        first = composition.publish_semantic_control()

        assert composition.publish_semantic_control() is first

    @pytest.mark.usefixtures("_published_registry")
    def test_the_current_verb_follows_the_configuration_rather_than_a_snapshot(
        self, file_semantic
    ) -> None:
        """The property a stored object could not have.

        ``current`` is read again after the merge has adopted something else,
        and answers with the new configuration — which is what lets the form
        show what is in force instead of what was in force when the application
        started.
        """
        control = composition.publish_semantic_control()
        assert control.current() is file_semantic

        composition._semantic_override = SemanticConfig(
            provider=SemanticProvider.OLLAMA, model="nomic-embed-text"
        )

        assert control.current().model == "nomic-embed-text"


class TestEveryProcessThatTouchesAVectorAdoptsTheStoredSettings:
    """One helper, named in every entry point, asserted from outside all four.

    ``_semantic_override`` is process state and starts out ``None``, so each of
    the four composition roots — the web application, the import worker,
    ``python -m app.embedding`` and ``python -m app.derive`` — has to read the
    row for itself. Two of them did not, and the symptom was silent at both
    ends: the embed command wrote ``embedding_model`` from the file's model
    while the pages searched under the stored one, and ``SEMANTIC_NEIGHBOURS``
    filters on exactly that field, so the search returned nothing and reported
    nothing.

    Structural on purpose. A behavioural test per entry point proves the three
    that exist today and says nothing about the fourth somebody adds next year;
    this one names the property — *the helper is what an entry point reaches
    for* — and a new command that grew its own ``try: await
    load_semantic_config()`` would have to be added to this list to pass, which
    is the moment to notice.
    """

    def test_the_helper_swallows_a_read_it_cannot_do(
        self, file_semantic, caplog, monkeypatch
    ) -> None:
        """The swallow lives here now, so the four callers share one policy."""

        async def refusing() -> SemanticConfig:
            raise OperationalError(
                "SELECT semantic_settings", {}, RuntimeError("no such table")
            )

        monkeypatch.setattr(composition, "load_semantic_config", refusing)

        with caplog.at_level(logging.ERROR, logger="app.composition"):
            asyncio.run(composition.adopt_semantic_settings())

        assert composition.semantic_config() is file_semantic
        assert "stored embedder settings" in caplog.text

    def test_every_entry_point_names_it(self) -> None:
        from app import derive, embedding, worker

        for module in (worker, embedding, derive):
            assert module.adopt_semantic_settings is composition.adopt_semantic_settings
