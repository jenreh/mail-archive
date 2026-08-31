"""The composition root: what the web application builds, and when."""

import functools
import importlib
import logging
import sys

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.registry import service_registry

from app import composition
from app.configuration import configure
from mailarc_analytics import AnalyticsConfig, AnalyticsReader
from mailarc_analytics.semantic import SemanticConfig, SemanticProvider, SemanticSearch
from mailarc_core import (
    ArchiveConfig,
    ArchiveReader,
    FalkorDBServer,
    GraphConfig,
    GraphServerMode,
)
from mailarc_core.graph.health import GraphHealth
from mailarc_core.mail.config import MailConfig
from mailarc_core.storage import StorageReader
from mailarc_google.source import GmailConfig
from mailarc_imap.source import ImapConfig
from mailarc_m365.source import M365Config
from mailarc_sync.engine import SyncConfig
from mailarc_ui.insights import analytics_reader as analytics_the_ui_sees
from mailarc_ui.insights.search import archive_search as search_the_ui_sees
from mailarc_ui.review import archive_reader as reader_the_ui_sees
from mailarc_ui.status import graph_health as health_the_ui_sees

GETTERS: dict[type, str] = {
    GraphConfig: "graph_config",
    SyncConfig: "sync_config",
    ArchiveConfig: "archive_config",
    AnalyticsConfig: "analytics_config",
    SemanticConfig: "semantic_config",
    MailConfig: "mail_config",
    GmailConfig: "google_config",
    ImapConfig: "imap_config",
    M365Config: "m365_config",
}
"""Every configuration object the root hands out, and the getter for each.

By name rather than by reference, so a test that monkeypatches a getter is not
racing a mapping built at import time — and so adding a provider is one line
here instead of one more branch in a chain of ``is`` comparisons.
"""

CONFIGS = tuple(GETTERS)


REGISTRY_LOGGER = "appkit_commons.registry"
"""Who says "overwriting" when a registration replaces one that was there."""


SLEEPER = (sys.executable, "-c", "import time; time.sleep(30)")
"""A child that outlives the test unless it is stopped."""

DIES = (sys.executable, "-c", "raise SystemExit(3)")
"""A child that is gone before the start returns."""


def _getter(config: type):
    return getattr(composition, GETTERS[config])


MEMOISED = (
    "graph_server",
    "graph_health",
    "provider_registry",
    "archive_reader",
    "analytics_reader",
    "semantic_embedder",
    "semantic_search",
    "storage_reader",
    "sync_worker",
)
"""Every ``lru_cache`` on the composition root, by name.

A list rather than two hand-written runs of ``cache_clear()`` calls: the two
runs had already been written twice and a handle added to one and not the other
leaks its first construction into every test after it — which is exactly the
kind of failure that shows up somewhere else entirely.
"""


def _clear_memoised() -> None:
    for name in MEMOISED:
        getattr(composition, name).cache_clear()


@pytest.fixture(autouse=True)
def _clear_caches():
    """The composition root memoises; each test needs a clean slate.

    ``_semantic_override`` is module state rather than a cache and is reset the
    same way: a test that adopted stored settings would otherwise decide what
    every later test's ``semantic_config()`` answers.
    """
    composition._semantic_override = None
    _clear_memoised()
    yield
    composition._semantic_override = None
    _clear_memoised()


@pytest.fixture
def _published_registry():
    """Publishing writes into the process-wide registry; put it back after."""
    registry = service_registry()
    saved = registry.snapshot()
    yield
    registry.restore(saved)


def _use_config(monkeypatch, mode: GraphServerMode) -> GraphConfig:
    config = GraphConfig(mode=mode, host="127.0.0.1", port=6379)
    monkeypatch.setattr(composition, "graph_config", lambda: config)
    return config


def _use_database(url: str) -> DatabaseConfig:
    """Put a database configuration in the registry, the way the app does.

    Through the registry rather than by monkeypatching :func:`_registered`:
    that one function answers for every configuration object the root hands
    out, so replacing it hands an ``ArchiveConfig`` question a ``DatabaseConfig``
    answer. Callers take ``_published_registry``, which puts the real one back.
    """
    config = DatabaseConfig.model_validate({"url_override": url})
    service_registry().register_as(DatabaseConfig, config)
    return config


def test_the_server_is_built_from_the_registered_configuration(monkeypatch) -> None:
    config = _use_config(monkeypatch, GraphServerMode.REMOTE)

    server = composition.graph_server()

    assert isinstance(server, FalkorDBServer)
    assert server.endpoint == config.endpoint


def test_the_server_is_a_singleton(monkeypatch) -> None:
    """One server per process — one per caller would leak a redis-server."""
    _use_config(monkeypatch, GraphServerMode.LOCAL)

    assert composition.graph_server() is composition.graph_server()


@pytest.mark.parametrize("config", CONFIGS, ids=lambda one: one.__name__)
def test_a_config_comes_from_the_registry(config) -> None:
    registry = service_registry()
    saved = registry.snapshot()
    registered = config()
    registry.register_as(config, registered)
    try:
        assert _getter(config)() is registered
    finally:
        registry.restore(saved)


@pytest.mark.parametrize("config", CONFIGS, ids=lambda one: one.__name__)
def test_configuring_the_application_registers_the_config(config) -> None:
    """The getters can only find what ``AppConfig`` actually carries."""
    configure()  # cached; importing `app` already ran it

    assert isinstance(_getter(config)(), config)


@pytest.mark.parametrize("config", CONFIGS, ids=lambda one: one.__name__)
def test_a_config_explains_itself_when_unregistered(config) -> None:
    """Against an empty registry, not against a stubbed ``get``.

    This used to patch ``get`` into returning ``None`` — which the real
    ``ServiceRegistry`` never does: it raises ``KeyError``. So the test passed
    while the branch it exercised was unreachable and the sentence it asserted
    never reached anybody; a caller in an un-configured process got a bare
    ``KeyError: 'Instance of type GraphConfig not found in registry'``. Emptying
    the registry asks the same question of the code that actually runs.
    """
    registry = composition.service_registry()
    saved = registry.snapshot()
    try:
        registry.restore({})
        with pytest.raises(RuntimeError, match=f"{config.__name__}.*configure"):
            _getter(config)()
    finally:
        registry.restore(saved)


def test_the_startup_error_comes_from_the_server(monkeypatch) -> None:
    _use_config(monkeypatch, GraphServerMode.REMOTE)
    composition.graph_server()._startup_error = "run `task tauri:vendor`"

    assert composition.graph_startup_error() == "run `task tauri:vendor`"


async def test_graph_status_reads_the_configured_server(monkeypatch) -> None:
    config = _use_config(monkeypatch, GraphServerMode.REMOTE)
    seen: list[GraphConfig] = []

    async def fake_read(cfg: GraphConfig) -> str:
        seen.append(cfg)
        return "status"

    monkeypatch.setattr(composition, "read_status_async", fake_read)

    assert await composition.graph_status() == "status"
    assert seen == [config]


class TestLifespan:
    @staticmethod
    def _recording_server(monkeypatch) -> list[str]:
        events: list[str] = []

        class Recording:
            async def start_async(self) -> None:
                events.append("start")

            async def stop_async(self) -> None:
                events.append("stop")

        recorder = Recording()
        monkeypatch.setattr(composition, "graph_server", lambda: recorder)
        return events

    async def test_starts_on_entry_and_stops_on_exit(self, monkeypatch) -> None:
        events = self._recording_server(monkeypatch)

        async with composition.graph_server_lifespan():
            assert events == ["start"]

        assert events == ["start", "stop"]

    async def test_a_failed_start_does_not_take_the_app_down(self, monkeypatch) -> None:
        """The page whose job is reporting server state is more useful up."""
        events: list[str] = []

        class Broken:
            async def start_async(self) -> None:
                raise RuntimeError("run `task tauri:vendor`")

            async def stop_async(self) -> None:
                events.append("stop")

        broken = Broken()
        monkeypatch.setattr(composition, "graph_server", lambda: broken)

        async with composition.graph_server_lifespan():
            pass

        assert events == ["stop"]

    async def test_stops_even_when_the_app_body_raises(self, monkeypatch) -> None:
        events = self._recording_server(monkeypatch)

        with pytest.raises(ValueError):
            async with composition.graph_server_lifespan():
                raise ValueError("app blew up")

        assert events == ["start", "stop"]


def test_the_reader_is_built_on_the_configured_stores(monkeypatch, tmp_path) -> None:
    """The review page must list what the worker wrote: same graph, same blob
    store. Read off the reader's own parts, because that is what would differ
    if a wire pointed elsewhere."""
    graph = _use_config(monkeypatch, GraphServerMode.REMOTE)
    archive = ArchiveConfig(store_dir=tmp_path / "blobs")
    monkeypatch.setattr(composition, "archive_config", lambda: archive)

    reader = composition.archive_reader()

    assert reader is composition.archive_reader()
    assert reader._blobs.root == archive.store_dir
    assert isinstance(reader._graph_session, functools.partial)
    assert reader._graph_session.args == (graph,)


@pytest.mark.usefixtures("_published_registry")
def test_the_ui_finds_the_reader_without_importing_the_app() -> None:
    """Same hand-over as the provider registry, asserted through the UI's own
    lookup because that is the code a broken one would break."""
    published = composition.publish_archive_reader()

    assert published is composition.archive_reader()
    assert reader_the_ui_sees() is published


@pytest.mark.usefixtures("_published_registry")
def test_publishing_the_reader_twice_leaves_one(caplog) -> None:
    first = composition.publish_archive_reader()

    with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
        assert composition.publish_archive_reader() is first

    assert service_registry().get(ArchiveReader) is first
    assert caplog.records == []


def test_the_analytics_reader_is_built_on_the_configured_graph(monkeypatch) -> None:
    """The same graph the archive reader reads and the rebuild writes to.

    The cross-check on the insights page holds a derived edge against the
    messages it came from, so a reader pointed at a second graph would not read
    as a bug — it would read as an archive that disagrees with itself. Asserted
    off the reader's own session factory, because that is the only part of it a
    wrong wire would show up in.
    """
    graph = _use_config(monkeypatch, GraphServerMode.REMOTE)

    reader = composition.analytics_reader()

    assert reader is composition.analytics_reader()
    assert isinstance(reader._graph_session, functools.partial)
    assert reader._graph_session.args == (graph,)


def test_the_analytics_reader_has_no_second_store(monkeypatch) -> None:
    """Everything a rebuild writes is a node or an edge — nothing on disk."""
    _use_config(monkeypatch, GraphServerMode.REMOTE)

    assert not hasattr(composition.analytics_reader(), "_blobs")


@pytest.mark.usefixtures("_published_registry")
def test_the_ui_finds_the_analytics_reader_without_importing_the_app() -> None:
    """Same hand-over as the archive reader, asserted through the UI's own
    lookup because that is the code a broken one would break."""
    published = composition.publish_analytics_reader()

    assert published is composition.analytics_reader()
    assert analytics_the_ui_sees() is published


@pytest.mark.usefixtures("_published_registry")
def test_publishing_the_analytics_reader_twice_leaves_one(caplog) -> None:
    first = composition.publish_analytics_reader()

    with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
        assert composition.publish_analytics_reader() is first

    assert service_registry().get(AnalyticsReader) is first
    assert caplog.records == []


class TestTheGraphHealth:
    """What ``/admin/status`` reads, and the reason it could leave ``app/``.

    ``GraphStatusState`` used to import :func:`graph_status` and
    :func:`graph_startup_error` from the composition root by name, which is a
    component importing the application. The façade is what replaced that, so
    the assertions are about the two halves reaching it intact.
    """

    def test_it_reads_the_configured_server(self, monkeypatch) -> None:
        config = _use_config(monkeypatch, GraphServerMode.REMOTE)

        health = composition.graph_health()

        assert health._config is config
        assert health._server is composition.graph_server()

    def test_it_is_a_singleton(self, monkeypatch) -> None:
        """Two would be two answers to "which server is being reported on"."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)

        assert composition.graph_health() is composition.graph_health()

    def test_the_startup_error_is_read_through_to_the_handle(self, monkeypatch) -> None:
        """Read through rather than copied at construction: the handle learns
        of a failure when the lifespan hook tries to start it, which is after
        this object exists."""
        _use_config(monkeypatch, GraphServerMode.LOCAL)
        health = composition.graph_health()

        composition.graph_server()._startup_error = "run `task tauri:vendor`"

        assert health.startup_error() == "run `task tauri:vendor`"

    @pytest.mark.usefixtures("_published_registry")
    def test_the_ui_finds_it_without_importing_the_app(self, monkeypatch) -> None:
        """Asserted through the UI's own lookup, because that is the code a
        broken hand-over would break."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)

        published = composition.publish_graph_health()

        assert published is composition.graph_health()
        assert health_the_ui_sees() is published

    @pytest.mark.usefixtures("_published_registry")
    def test_publishing_it_twice_leaves_one(self, monkeypatch, caplog) -> None:
        _use_config(monkeypatch, GraphServerMode.REMOTE)
        first = composition.publish_graph_health()

        with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
            assert composition.publish_graph_health() is first

        assert service_registry().get(GraphHealth) is first
        assert caplog.records == []


class TestTheStorageReader:
    """The three paths one archive lives in, and the only module that has all
    three.

    The mailstore comes from ``mailarc_core.archive``, the graph directory from
    ``mailarc_core.graph`` and the database file from appkit, so a reader built
    anywhere else would be a reader built from a config somebody guessed at.
    """

    def test_it_measures_the_configured_paths(self, monkeypatch, tmp_path) -> None:
        graph = GraphConfig(mode=GraphServerMode.LOCAL, data_dir=tmp_path / "falkordb")
        archive = ArchiveConfig(store_dir=tmp_path / "blobs")
        monkeypatch.setattr(composition, "graph_config", lambda: graph)
        monkeypatch.setattr(composition, "archive_config", lambda: archive)

        paths = composition.storage_reader()._paths

        assert paths["Mailstore"] == archive.store_dir
        assert paths["Graph"] == graph.data_dir

    @pytest.mark.usefixtures("_published_registry")
    def test_a_database_in_memory_contributes_no_path(self) -> None:
        """``sqlite+aiosqlite:///:memory:`` is what the test profile uses, and a
        row reading "Database — 0 bytes" would describe a file that does not
        exist."""
        _use_database("sqlite+aiosqlite:///:memory:")

        assert "Database" not in composition.storage_reader()._paths

    @pytest.mark.usefixtures("_published_registry")
    def test_a_database_on_disk_is_measured_with_the_rest(self, tmp_path) -> None:
        database = tmp_path / "mail-archive.db"
        _use_database(f"sqlite+aiosqlite:///{database}")

        assert composition.storage_reader()._paths["Database"] == database

    def test_it_is_a_singleton(self) -> None:
        assert composition.storage_reader() is composition.storage_reader()

    @pytest.mark.usefixtures("_published_registry")
    def test_the_ui_finds_it_without_importing_the_app(self) -> None:
        published = composition.publish_storage_reader()

        assert published is composition.storage_reader()
        assert service_registry().get(StorageReader) is published

    @pytest.mark.usefixtures("_published_registry")
    def test_publishing_it_twice_leaves_one(self, caplog) -> None:
        first = composition.publish_storage_reader()

        with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
            assert composition.publish_storage_reader() is first

        assert service_registry().get(StorageReader) is first
        assert caplog.records == []


class TestTheSearch:
    """The panel at ``/insights`` reads through this and nothing else.

    Its absence was invisible for a whole phase because every test in
    ``mailarc-ui`` registers a search of its own, so these three assertions are
    the ones that turn "nobody published it" from a red box on a live page into
    a red test — the same three the analytics reader already carries.
    """

    def test_it_is_built_on_the_configured_graph(self, monkeypatch) -> None:
        graph = _use_config(monkeypatch, GraphServerMode.REMOTE)

        search = composition.semantic_search()

        assert isinstance(search, SemanticSearch)
        assert isinstance(search._graph_session, functools.partial)
        assert search._graph_session.args == (graph,)

    def test_it_is_a_singleton(self, monkeypatch) -> None:
        """The embedder behind it holds an ``httpx`` pool; a second search
        would be a second pool nobody closes."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)

        assert composition.semantic_search() is composition.semantic_search()

    def test_it_takes_its_embedder_from_the_registered_configuration(
        self, monkeypatch
    ) -> None:
        """The half a YAML profile can reach. With no ``semantic`` section on
        ``AppConfig`` this could only ever be ``none``, whatever was written
        in ``configuration/config.yaml``."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)
        registry = service_registry()
        saved = registry.snapshot()
        registry.register_as(
            SemanticConfig, SemanticConfig(provider=SemanticProvider.OLLAMA)
        )
        try:
            assert composition.semantic_search().available
        finally:
            registry.restore(saved)

    def test_it_is_off_by_default_rather_than_broken(self, monkeypatch) -> None:
        """``provider=none`` is the supported default, and full text still
        answers through the same object."""
        _use_config(monkeypatch, GraphServerMode.REMOTE)

        assert not composition.semantic_search().available

    @pytest.mark.usefixtures("_published_registry")
    def test_the_ui_finds_it_without_importing_the_app(self) -> None:
        published = composition.publish_semantic_search()

        assert published is composition.semantic_search()
        assert search_the_ui_sees() is published

    @pytest.mark.usefixtures("_published_registry")
    def test_publishing_it_twice_leaves_one(self, caplog) -> None:
        first = composition.publish_semantic_search()

        with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
            assert composition.publish_semantic_search() is first

        assert service_registry().get(SemanticSearch) is first
        assert caplog.records == []


def test_the_worker_handle_is_a_singleton() -> None:
    """A second handle would be a second child claiming the same jobs."""
    assert composition.sync_worker() is composition.sync_worker()


class TestWorkerProcess:
    """The child itself, against real processes: a fake proves nothing here."""

    async def test_starts_a_child_and_takes_it_down_again(self) -> None:
        """Through the async pair, because that is what the lifespan calls."""
        worker = composition.WorkerProcess(SLEEPER)

        await worker.start_async()
        child = worker._process
        assert worker.running
        assert worker.startup_error is None

        await worker.stop_async()

        assert not worker.running
        assert child is not None
        assert child.poll() is not None, "the child outlived the stop"

    def test_a_child_that_dies_on_the_spot_is_a_failed_start(self) -> None:
        """Nothing listens on a port here, so exiting is the only symptom."""
        worker = composition.WorkerProcess(DIES)

        with pytest.raises(RuntimeError, match="code 3"):
            worker.start()

        assert worker.startup_error is not None
        assert not worker.running

    def test_stopping_one_that_never_started_is_allowed(self) -> None:
        """A lifespan whose start failed still runs its finally."""
        composition.WorkerProcess(SLEEPER).stop()

    def test_the_command_names_a_module_that_can_be_run(self) -> None:
        """`task sync:worker` runs the same module by hand — a typo here would
        only show up as a child that dies at every application start."""
        module = importlib.import_module(composition.WORKER_COMMAND[-1])

        assert callable(module.main)


class TestSyncWorkerLifespan:
    @staticmethod
    def _supervised(monkeypatch, supervise: bool = True) -> list[str]:
        monkeypatch.setattr(
            composition, "sync_config", lambda: SyncConfig(supervise_worker=supervise)
        )
        events: list[str] = []

        class Recording:
            async def start_async(self) -> None:
                events.append("start")

            async def stop_async(self) -> None:
                events.append("stop")

        recorder = Recording()
        monkeypatch.setattr(composition, "sync_worker", lambda: recorder)
        return events

    async def test_starts_on_entry_and_stops_on_exit(self, monkeypatch) -> None:
        events = self._supervised(monkeypatch)

        async with composition.sync_worker_lifespan():
            assert events == ["start"]

        assert events == ["start", "stop"]

    async def test_a_failed_start_does_not_take_the_app_down(self, monkeypatch) -> None:
        """A job simply waits in the queue; the archive is readable meanwhile."""
        monkeypatch.setattr(composition, "sync_config", SyncConfig)
        events: list[str] = []

        class Broken:
            async def start_async(self) -> None:
                raise RuntimeError("the sync worker exited immediately")

            async def stop_async(self) -> None:
                events.append("stop")

        broken = Broken()
        monkeypatch.setattr(composition, "sync_worker", lambda: broken)

        async with composition.sync_worker_lifespan():
            pass

        assert events == ["stop"]

    async def test_stops_even_when_the_app_body_raises(self, monkeypatch) -> None:
        events = self._supervised(monkeypatch)

        with pytest.raises(ValueError):
            async with composition.sync_worker_lifespan():
                raise ValueError("app blew up")

        assert events == ["start", "stop"]

    async def test_supervision_can_be_handed_to_docker(self, monkeypatch) -> None:
        """Off under systemd: a second copy would claim the same jobs."""
        events = self._supervised(monkeypatch, supervise=False)

        async with composition.sync_worker_lifespan():
            pass

        assert events == []
