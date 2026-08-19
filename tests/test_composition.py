"""The composition root: what the web application builds, and when."""

import importlib
import sys

import pytest
from appkit_commons.registry import service_registry

from app import composition
from app.configuration import configure
from mailarc_core import (
    ArchiveConfig,
    FalkorDBServer,
    GraphConfig,
    GraphServerMode,
)
from mailarc_core.mail.config import MailConfig
from mailarc_core.mail.model import MailProvider
from mailarc_sync.engine import FakeMailSource, SyncConfig

CONFIGS = (GraphConfig, SyncConfig, ArchiveConfig, MailConfig)
"""Every configuration object the root hands out, and the getter for each."""

SLEEPER = (sys.executable, "-c", "import time; time.sleep(30)")
"""A child that outlives the test unless it is stopped."""

DIES = (sys.executable, "-c", "raise SystemExit(3)")
"""A child that is gone before the start returns."""


def _getter(config: type):
    return {
        GraphConfig: composition.graph_config,
        SyncConfig: composition.sync_config,
        ArchiveConfig: composition.archive_config,
        MailConfig: composition.mail_config,
    }[config]


@pytest.fixture(autouse=True)
def _clear_caches():
    """The composition root memoises; each test needs a clean slate."""
    composition.graph_server.cache_clear()
    composition.provider_registry.cache_clear()
    composition.sync_worker.cache_clear()
    yield
    composition.graph_server.cache_clear()
    composition.provider_registry.cache_clear()
    composition.sync_worker.cache_clear()


def _use_config(monkeypatch, mode: GraphServerMode) -> GraphConfig:
    config = GraphConfig(mode=mode, host="127.0.0.1", port=6379)
    monkeypatch.setattr(composition, "graph_config", lambda: config)
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
def test_a_config_explains_itself_when_unregistered(config, monkeypatch) -> None:
    monkeypatch.setattr(composition.service_registry(), "get", lambda _: None)

    with pytest.raises(RuntimeError, match=f"{config.__name__}.*configure"):
        _getter(config)()


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


def test_the_registry_can_build_the_fake_mailbox() -> None:
    """The one provider phase 2 ships — Gmail joins it in phase 3."""
    registry = composition.provider_registry()

    assert registry.supports(MailProvider.FAKE)
    assert [one.provider for one in registry.descriptors()] == [MailProvider.FAKE]

    built = registry.factory_for(MailProvider.FAKE)(None, "/mailboxes/exported")

    assert isinstance(built, FakeMailSource)


def test_the_registry_is_a_singleton() -> None:
    """A second registry would be a second answer to "which providers exist"."""
    assert composition.provider_registry() is composition.provider_registry()


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
