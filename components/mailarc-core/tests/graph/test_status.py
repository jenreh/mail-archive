"""Status reading, on FalkorDB and on a backend that has no redis at all.

Most of this file drives the genuine :class:`runic.ogm.FalkorDBDriver` — only
the redis handle below it is faked — so it exercises the wiring status really
uses rather than a mock of it. `TestOtherBackends` at the bottom does the
opposite: a bare :class:`runic.ogm.GraphDriver` and nothing vendor-shaped,
which is what makes the backend-independence claim testable.
"""

from typing import Never

import pytest
from runic.ogm import FalkorDBDriver, GraphDialect, Neo4jDialect

from mailarc_core.graph import status
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import (
    GraphBackend,
    GraphInfo,
    GraphServerMode,
    GraphServerStatus,
)

INFO = {
    "redis_version": "8.10.1",
    "uptime_in_seconds": "120",
    "used_memory_human": "1.41M",
    "connected_clients": "2",
    "total_commands_processed": "37",
}

GRAPH_NAME = "mail-archive"


class FakeResult:
    """Stands in for ``falkordb.QueryResult``; runic reads ``result_set``."""

    def __init__(self, value):
        self.result_set = [[value]] if value is not None else []
        self.header = [[1, "count"]]


class FakeGraph:
    def __init__(self, nodes: int | None, edges: int | None):
        self._counts = {"n)": nodes, "r)": edges}

    def query(self, query: str, params=None) -> FakeResult:  # noqa: ARG002
        # `count(n)` -> nodes, `count(r)` -> edges
        key = "n)" if "count(n)" in query else "r)"
        return FakeResult(self._counts[key])


class ExplodingGraph:
    def query(self, query: str, params=None) -> Never:  # noqa: ARG002
        raise RuntimeError("graph is busy")


class FakeConnection:
    def __init__(self, info=None, modules=None, fail_modules=False):
        self._info = INFO if info is None else info
        self._modules = modules
        self._fail_modules = fail_modules

    def info(self) -> dict:
        return self._info

    def module_list(self) -> list | None:
        if self._fail_modules:
            raise RuntimeError("MODULE LIST refused")
        return self._modules


class FakeDB:
    def __init__(self, connection, graphs=None, fail_list=False):
        self.connection = connection
        self._graphs = graphs or {}
        self._fail_list = fail_list
        self.closed = False

    def list_graphs(self) -> list:
        if self._fail_list:
            raise RuntimeError("GRAPH.LIST refused")
        return list(self._graphs)

    def select_graph(self, name):  # noqa: ANN201 - returns a fake graph
        return self._graphs.get(name) or FakeGraph(nodes=0, edges=0)

    def close(self) -> None:
        self.closed = True


def _driver(db: FakeDB) -> FalkorDBDriver:
    """What `client.connect` would build, over a fake handle."""
    return FalkorDBDriver(db.select_graph(GRAPH_NAME), db)


@pytest.fixture
def config() -> GraphConfig:
    return GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="127.0.0.1",
        port=6379,
        graph_name=GRAPH_NAME,
    )


def _read(config: GraphConfig, db, monkeypatch) -> GraphServerStatus:
    """Read a status against a fake connection."""
    monkeypatch.setattr(status, "connect", lambda _config: _driver(db))
    return status.read_status(config)


def test_reports_a_reachable_server_with_versions_and_metrics(
    config, monkeypatch
) -> None:
    db = FakeDB(
        FakeConnection(modules=[{"name": "graph", "ver": 42003}]),
        graphs={GRAPH_NAME: FakeGraph(nodes=3, edges=2)},
    )

    result = _read(config, db, monkeypatch)

    assert result.reachable is True
    assert result.redis_version == "8.10.1"
    assert result.falkordb_version == "4.20.3"
    assert result.vector_knn_supported is True
    assert result.endpoint == "127.0.0.1:6379"
    assert result.latency_ms is not None
    assert result.metrics is not None
    assert result.metrics.uptime_seconds == 120
    assert result.metrics.used_memory_human == "1.41M"
    assert result.graphs == (GraphInfo(name=GRAPH_NAME, node_count=3, edge_count=2),)
    assert db.closed is True


def test_a_connection_failure_becomes_an_unreachable_status(
    config, monkeypatch
) -> None:
    def boom(_config):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(status, "connect", boom)
    result = status.read_status(config)

    assert result.reachable is False
    assert "ConnectionError" in (result.error or "")
    assert "Connection refused" in (result.error or "")
    assert result.graphs == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (42003, "4.20.3"),
        (40203, "4.2.3"),
        (30105, "3.1.5"),
    ],
)
def test_module_version_int_is_rendered_as_dotted(
    config, monkeypatch, raw, expected
) -> None:
    db = FakeDB(FakeConnection(modules=[{"name": "graph", "ver": raw}]))

    result = _read(config, db, monkeypatch)

    assert result.falkordb_version == expected


def test_module_list_flat_list_shape_is_understood(config, monkeypatch) -> None:
    db = FakeDB(FakeConnection(modules=[["name", "graph", "ver", 42003, "path", "x"]]))

    result = _read(config, db, monkeypatch)

    assert result.falkordb_version == "4.20.3"


def test_the_graph_module_is_found_past_redis_builtins(config, monkeypatch) -> None:
    # Real MODULE LIST output from Redis 8.10.1: the built-in `vectorset`
    # module is listed before FalkorDB's `graph`.
    db = FakeDB(
        FakeConnection(
            modules=[
                {"name": "vectorset", "ver": 1, "path": "", "args": []},
                {"name": "graph", "ver": 42003, "path": "/x/falkordb.so", "args": []},
            ]
        )
    )

    result = _read(config, db, monkeypatch)

    assert result.falkordb_version == "4.20.3"
    assert result.vector_knn_supported is True


def test_missing_module_list_still_yields_a_reachable_status(
    config, monkeypatch
) -> None:
    db = FakeDB(FakeConnection(fail_modules=True))

    result = _read(config, db, monkeypatch)

    assert result.reachable is True
    assert result.falkordb_version is None
    assert result.vector_knn_supported is False


def test_graph_list_failure_leaves_the_rest_of_the_status_intact(
    config, monkeypatch
) -> None:
    db = FakeDB(
        FakeConnection(modules=[{"name": "graph", "ver": 42003}]), fail_list=True
    )

    result = _read(config, db, monkeypatch)

    assert result.reachable is True
    assert result.graphs == ()


def test_one_unreadable_graph_does_not_blank_the_others(config, monkeypatch) -> None:
    db = FakeDB(
        FakeConnection(modules=[{"name": "graph", "ver": 42003}]),
        graphs={"good": FakeGraph(nodes=1, edges=0), "bad": ExplodingGraph()},
    )

    result = _read(config, db, monkeypatch)

    by_name = {g.name: g for g in result.graphs}
    assert by_name["good"].node_count == 1
    assert by_name["bad"].node_count == -1


def test_an_empty_graph_counts_as_zero(config, monkeypatch) -> None:
    db = FakeDB(
        FakeConnection(modules=[{"name": "graph", "ver": 42003}]),
        graphs={"empty": FakeGraph(nodes=None, edges=None)},
    )

    result = _read(config, db, monkeypatch)

    assert result.graphs[0].node_count == 0
    assert result.graphs[0].edge_count == 0


def test_every_graph_on_the_server_is_counted_not_just_the_configured_one(
    config, monkeypatch
) -> None:
    """A driver addresses one graph; the inventory must still cover them all."""
    db = FakeDB(
        FakeConnection(modules=[{"name": "graph", "ver": 42003}]),
        graphs={
            GRAPH_NAME: FakeGraph(nodes=3, edges=2),
            "scratch": FakeGraph(nodes=7, edges=5),
        },
    )

    result = _read(config, db, monkeypatch)

    by_name = {g.name: g for g in result.graphs}
    assert by_name[GRAPH_NAME].node_count == 3
    assert by_name["scratch"].node_count == 7
    assert by_name["scratch"].edge_count == 5


def test_remote_mode_is_carried_into_the_status(monkeypatch) -> None:
    config = GraphConfig(mode=GraphServerMode.REMOTE, host="graph.internal", port=6380)
    db = FakeDB(FakeConnection(modules=[{"name": "graph", "ver": 42003}]))

    result = _read(config, db, monkeypatch)

    assert result.mode is GraphServerMode.REMOTE
    assert result.endpoint == "graph.internal:6380"


class TestReadStatusAsync:
    """`read_status_async` only moves the blocking read off the event loop."""

    async def test_returns_what_the_blocking_read_reports(
        self, config, monkeypatch
    ) -> None:
        db = FakeDB(FakeConnection(modules=[{"name": "graph", "ver": 42003}]))
        monkeypatch.setattr(status, "connect", lambda _config: _driver(db))

        result = await status.read_status_async(config)

        assert result.reachable is True
        assert result.vector_knn_supported is True

    async def test_passes_through_an_unreachable_server(
        self, config, monkeypatch
    ) -> None:
        def boom(_config):
            raise ConnectionError("refused")

        monkeypatch.setattr(status, "connect", boom)

        result = await status.read_status_async(config)

        assert result.reachable is False
        assert "refused" in (result.error or "")

    async def test_each_call_re_reads_the_server(self, config, monkeypatch) -> None:
        calls: list[GraphConfig] = []

        def record(cfg: GraphConfig) -> FalkorDBDriver:
            calls.append(cfg)
            return _driver(FakeDB(FakeConnection()))

        monkeypatch.setattr(status, "connect", record)

        await status.read_status_async(config)
        await status.read_status_async(config)

        assert len(calls) == 2


class CypherResult:
    """What a non-FalkorDB driver hands back: normalised rows and columns."""

    def __init__(self, value: int):
        self.rows = [[value]]
        self.columns = ["value"]


class CypherDriver:
    """A bare `GraphDriver`. No redis handle, so no admin command can work."""

    def __init__(self, nodes: int = 3, edges: int = 2, unreachable: bool = False):
        self._counts = {"n)": nodes, "r)": edges}
        self._unreachable = unreachable
        self.executed: list[str] = []
        self.closed = False

    @property
    def dialect(self) -> GraphDialect:
        return Neo4jDialect()

    def execute(self, cypher: str, params: dict) -> CypherResult:  # noqa: ARG002
        if self._unreachable:
            raise ConnectionError("Connection refused")
        self.executed.append(cypher)
        if "count(" not in cypher:
            return CypherResult(1)
        return CypherResult(self._counts["n)" if "count(n)" in cypher else "r)"])

    def close(self) -> None:
        self.closed = True


class TestOtherBackends:
    """A Cypher server that is not FalkorDB still gets a usable status."""

    @pytest.fixture
    def config(self) -> GraphConfig:
        return GraphConfig(
            mode=GraphServerMode.REMOTE,
            backend=GraphBackend.NEO4J,
            host="neo.internal",
            port=7687,
            graph_name="mail-archive",
        )

    def test_reachability_is_settled_by_cypher_not_by_info(
        self, config, monkeypatch
    ) -> None:
        driver = CypherDriver()
        monkeypatch.setattr(status, "connect", lambda _config: driver)

        result = status.read_status(config)

        assert result.reachable is True
        assert result.endpoint == "neo.internal:7687"
        assert result.latency_ms is not None
        assert driver.executed[0] == "RETURN 1"

    def test_the_redis_only_facts_are_left_empty_rather_than_faked(
        self, config, monkeypatch
    ) -> None:
        monkeypatch.setattr(status, "connect", lambda _config: CypherDriver())

        result = status.read_status(config)

        assert result.reachable is True, result.error
        assert result.redis_version is None
        assert result.falkordb_version is None
        assert result.metrics is None
        assert result.vector_knn_supported is False

    def test_the_configured_graph_is_the_whole_inventory(
        self, config, monkeypatch
    ) -> None:
        """One driver, one graph — there is no GRAPH.LIST to enumerate."""
        monkeypatch.setattr(status, "connect", lambda _config: CypherDriver())

        result = status.read_status(config)

        assert result.graphs == (
            GraphInfo(name="mail-archive", node_count=3, edge_count=2),
        )

    def test_a_server_that_refuses_becomes_an_unreachable_status(
        self, config, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            status, "connect", lambda _config: CypherDriver(unreachable=True)
        )

        result = status.read_status(config)

        assert result.reachable is False
        assert "Connection refused" in (result.error or "")
        assert result.graphs == ()

    def test_the_driver_is_released_afterwards(self, config, monkeypatch) -> None:
        driver = CypherDriver()
        monkeypatch.setattr(status, "connect", lambda _config: driver)

        status.read_status(config)

        assert driver.closed is True
