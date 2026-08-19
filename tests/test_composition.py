"""The composition root: what the web application builds, and when."""

import pytest

from app import composition
from mailarc_core import FalkorDBServer, GraphConfig, GraphServerMode


@pytest.fixture(autouse=True)
def _clear_caches():
    """The composition root memoises; each test needs a clean slate."""
    composition.graph_server.cache_clear()
    yield
    composition.graph_server.cache_clear()


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


def test_graph_config_explains_itself_when_unregistered(monkeypatch) -> None:
    monkeypatch.setattr(composition.service_registry(), "get", lambda _: None)

    with pytest.raises(RuntimeError, match="configure"):
        composition.graph_config()


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
