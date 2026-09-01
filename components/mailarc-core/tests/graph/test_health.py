"""Tests for :class:`mailarc_core.graph.health.GraphHealth`.

No server is started here. `GraphHealth` is a two-method façade whose whole
purpose is that a state class can hold *it* instead of holding the composition
root, so what has to be proved is that it forwards to the right things: the
snapshot comes from :func:`read_status_async` over the config it was built
with, and the startup error comes off the server handle rather than being
remembered separately.

:func:`read_status_async` is stubbed and the server is a real, never-started
:class:`FalkorDBServer` — building one only assigns attributes.
"""

from datetime import UTC, datetime

import pytest

from mailarc_core.graph import health
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.health import GraphHealth
from mailarc_core.graph.model import GraphServerMode, GraphServerStatus
from mailarc_core.graph.server import FalkorDBServer

CHECKED_AT = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def config() -> GraphConfig:
    return GraphConfig(mode=GraphServerMode.REMOTE, host="graph.internal", port=6380)


def snapshot(config: GraphConfig) -> GraphServerStatus:
    return GraphServerStatus.unreachable(
        mode=config.mode,
        endpoint=config.endpoint,
        checked_at=CHECKED_AT,
        error="ConnectionError: refused",
    )


class TestStatus:
    async def test_it_reads_a_snapshot_over_the_config_it_holds(
        self, config, monkeypatch
    ) -> None:
        asked: list[GraphConfig] = []

        async def fake_read(passed: GraphConfig) -> GraphServerStatus:
            asked.append(passed)
            return snapshot(passed)

        monkeypatch.setattr(health, "read_status_async", fake_read)

        status = await GraphHealth(config, FalkorDBServer(config)).status()

        assert asked == [config]
        assert status.endpoint == "graph.internal:6380"
        assert status.reachable is False

    async def test_an_unreachable_server_is_a_status_and_not_an_error(
        self, config, monkeypatch
    ) -> None:
        """The reason the panel can render an outage at all — no raise here."""

        async def fake_read(passed: GraphConfig) -> GraphServerStatus:
            return snapshot(passed)

        monkeypatch.setattr(health, "read_status_async", fake_read)

        status = await GraphHealth(config, FalkorDBServer(config)).status()

        assert status.error == "ConnectionError: refused"


class TestStartupError:
    def test_a_server_that_started_cleanly_has_nothing_to_report(self, config) -> None:
        assert GraphHealth(config, FalkorDBServer(config)).startup_error() is None

    def test_it_reports_whatever_the_server_handle_reports(self, config) -> None:
        """Read through, not copied: the handle is the one that learns of it."""
        server = FalkorDBServer(config)
        graph_health = GraphHealth(config, server)

        server._startup_error = "the vendored falkordb.so is missing"

        assert graph_health.startup_error() == "the vendored falkordb.so is missing"
