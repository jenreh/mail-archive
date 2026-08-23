"""The embedder form's world: a database, a composition root and a graph.

A shared module rather than a ``conftest``, for the reason
:mod:`insights_archive` is one: what is below belongs to the two embedder test
files and to no others, and a ``conftest`` in this directory would put a
``sessions`` and a ``state`` in scope for the accounts, import, review and
insights tests that have their own idea of what those words mean.

The interesting piece is :class:`Composition`. It is ``app/composition.py`` in
twenty lines and it contains the *real* merge —
:meth:`~mailarc_analytics.semantic.config.SemanticOverrides.applied_to` — so a
test can follow a save all the way to what the archive would then build an
embedder from. A double that merely recorded the last call would prove that the
form wrote a row and nothing about whether the row means anything.

Everything else is a stand-in for something a test may not have: a real graph
behind the vector count, and appkit's session factory.
"""

import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from cryptography.fernet import Fernet
from insights_archive import FakeUser, signed_in_as
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mailarc_analytics.semantic import (
    SemanticConfig,
    SemanticControl,
    SemanticOverrides,
    SemanticProvider,
    SemanticSearch,
    VectorCoverage,
)
from mailarc_core.database.entities import SEMANTIC_SETTINGS_ID, SemanticSettingsEntity
from mailarc_ui.embedder import EmbedderSettingsState

STATE_MODULE = "mailarc_ui.embedder.state"
READS_MODULE = "mailarc_ui.embedder.reads"
"""Both halves of the page open sessions, so both are pointed at the test's.

``mailarc_ui.embedder.reads`` holds the writes and the graph read;
``mailarc_ui.embedder.state`` still opens one of its own for the two handlers
that write a single column. Patching only the first left the second reaching
appkit's real factory — which failed loudly here and would not have anywhere
else."""

SECRET = "sk-this-key-must-never-reach-a-browser"  # noqa: S105 — a fixture value
"""The key every write-only assertion looks for.

Distinctive on purpose: the check is a substring search over everything the
state would send, so the value has to be one nothing else could produce.
"""


class Sessions:
    """The state's database, with appkit's transaction contract.

    Commit when the block leaves cleanly, roll back when it does not — which is
    the half that makes "a failed key write takes the settings with it"
    testable at all.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    def __call__(self) -> Any:
        return self._open()

    def _open(self) -> Any:
        @contextlib.asynccontextmanager
        async def opened() -> AsyncIterator[AsyncSession]:
            async with self._factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        return opened()


class Composition:
    """``app/composition.py`` in twenty lines: it merges, and it re-merges.

    A stand-in with the *real* merge inside it —
    :meth:`SemanticOverrides.applied_to` — because what these tests are about
    is what the archive ends up using after somebody presses Save, and a fake
    that just remembered the last call would prove nothing about precedence.

    It reads the row with ``session.get`` rather than through the repository,
    so that
    :meth:`~mailarc_core.database.repositories.SemanticSettingsRepository.load`
    — the one read that hands back a decrypted key — is left belonging to
    nobody but the store itself, and a test can watch whether the state ever
    reaches for it.
    """

    def __init__(self, configured: SemanticConfig, sessions: Sessions) -> None:
        self._configured = configured
        self._sessions = sessions
        self.effective = configured
        self.reloads = 0
        self.breaks = False
        self.reindexes = 0
        self.cleared = 0

    def current(self) -> SemanticConfig:
        return self.effective

    async def reload(self) -> SemanticConfig:
        self.reloads += 1
        if self.breaks:
            raise ConnectionError("the embedder host did not answer")
        async with self._sessions() as session:
            stored = await session.get(SemanticSettingsEntity, SEMANTIC_SETTINGS_ID)
            overrides = (
                SemanticOverrides()
                if stored is None
                else SemanticOverrides(
                    provider=stored.provider,
                    model=stored.model,
                    dimension=stored.dimension,
                    base_url=stored.base_url,
                    api_key=stored.api_key,
                )
            )
        self.effective = overrides.applied_to(self._configured)
        return self.effective

    async def reindex(self) -> int:
        """Rebuild the index, as the composition root would. Records the call.

        Counted rather than performed: what the settings page owes its reader
        is that pressing the button reaches the composition root exactly once
        and that what comes back is reported. Whether an HNSW index is really
        resized is proved next door, against a real FalkorDB, in
        ``test_semantic_indexing_local.py``.
        """
        self.reindexes += 1
        if self.breaks:
            raise RuntimeError("the graph is down")
        return self.cleared


class StubSearch:
    """A search answering the two questions this page asks it about the graph.

    ``index`` is the length the *live* vector index carries, which is a
    different number from the configured one and is allowed to disagree with
    it — that disagreement is the whole subject of ``index_advice``. Both
    answers fail together through ``error``, because they come off the same
    graph and a settings page has to work when it is not running.
    """

    def __init__(
        self, embedded: int = 0, error: Exception | None = None, index: int = 768
    ) -> None:
        self.embedded = embedded
        self.error = error
        self.index = index
        self.asked = 0

    def coverage(self) -> VectorCoverage:
        self.asked += 1
        if self.error is not None:
            raise self.error
        return VectorCoverage(model="in-force", total=100, embedded=self.embedded)

    def index_dimension(self) -> int:
        if self.error is not None:
            raise self.error
        return self.index


@pytest.fixture
def keyed() -> Iterator[None]:
    """A registry holding the Fernet key ``EncryptedString`` reads at write time."""
    services = service_registry()
    saved = services.snapshot()
    services.register_as(
        DatabaseConfig,
        DatabaseConfig.model_validate(
            {"encryption_key": Fernet.generate_key().decode()}
        ),
    )
    yield
    services.restore(saved)


@pytest.fixture
async def sessions(tmp_path: Path, keyed: None) -> AsyncIterator[Sessions]:
    """The settings table, on a file of its own."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    opened = Sessions(async_sessionmaker(engine, expire_on_commit=False))
    with (
        patch(f"{STATE_MODULE}.get_asyncdb_session", opened),
        patch(f"{READS_MODULE}.get_asyncdb_session", opened),
    ):
        yield opened
    await engine.dispose()


@pytest.fixture
def configured() -> SemanticConfig:
    """What the configuration file says on a default installation: nothing.

    Built with explicit keyword arguments rather than from the environment,
    because the point of the fresh-installation test is the *defaults* and a
    developer's ``.env`` must not be able to move them.
    """
    return SemanticConfig(provider=SemanticProvider.NONE, model="", dimension=768)


@pytest.fixture
def composition(
    configured: SemanticConfig, sessions: Sessions
) -> Iterator[Composition]:
    """The composition root's two verbs, left where the state looks for them."""
    root = Composition(configured, sessions)
    services = service_registry()
    saved = services.snapshot()
    services.register_as(
        SemanticControl,
        SemanticControl(current=root.current, reload=root.reload, reindex=root.reindex),
    )
    yield root
    services.restore(saved)


@pytest.fixture
def searching(composition: Composition) -> StubSearch:
    """A published search, so the vector count is answerable.

    No teardown of its own: it registers into the snapshot ``composition``
    already took, and that fixture restores the whole registry afterwards.
    """
    stub = StubSearch()
    service_registry().register_as(SemanticSearch, cast(SemanticSearch, stub))
    return stub


@pytest.fixture
def state(
    composition: Composition, monkeypatch: pytest.MonkeyPatch
) -> EmbedderSettingsState:
    """The form, opened by an administrator.

    Signed in deliberately: this page points the archive's embedder at a host
    and holds the credential it is used with, and ``TestEveryHandlerIsGated``
    is where the refusal is exercised instead.
    """
    instance = EmbedderSettingsState()
    signed_in_as(instance, FakeUser(is_admin=True), monkeypatch)
    return instance


async def drive(handler: Any, state: EmbedderSettingsState) -> None:
    """Drive a background handler without Reflex's state lock under it."""
    with (
        patch.object(EmbedderSettingsState, "__aenter__", AsyncMock()),
        patch.object(EmbedderSettingsState, "__aexit__", AsyncMock(return_value=False)),
    ):
        await handler.fn(state)


async def load_form(state: EmbedderSettingsState) -> None:
    await drive(EmbedderSettingsState.load, state)


async def save_form(state: EmbedderSettingsState) -> None:
    await drive(EmbedderSettingsState.save, state)


async def clear_key(state: EmbedderSettingsState) -> None:
    await drive(EmbedderSettingsState.clear_api_key, state)


async def reset_form(state: EmbedderSettingsState) -> None:
    await drive(EmbedderSettingsState.use_configuration_file, state)


async def stored_row(sessions: Sessions) -> SemanticSettingsEntity | None:
    async with sessions() as session:
        return await session.get(SemanticSettingsEntity, SEMANTIC_SETTINGS_ID)


def everything_the_browser_would_see(state: EmbedderSettingsState) -> str:
    """Every var this state would send, rendered as one searchable string.

    Over ``EmbedderSettingsState.vars`` rather than over ``__dict__``, because
    that collection *is* the set Reflex ships: a backend-only var is absent
    from it by construction, which is exactly the property being relied on.
    """
    return json.dumps(
        {name: getattr(state, name, None) for name in EmbedderSettingsState.vars},
        default=str,
    )
