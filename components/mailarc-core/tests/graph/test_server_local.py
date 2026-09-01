"""Integration tests against the real vendored FalkorDB.

Skipped unless `task tauri:vendor` has produced the runtime, so a checkout
without it (CI, a fresh clone) still runs green.

The server fixture is SESSION scoped and tears itself down explicitly. A
function-scoped fixture would spawn one redis-server per test and leave every
one of them to be reaped at interpreter exit, which turns a fast suite into a
multi-minute hang with 0% CPU. Tests isolate themselves with unique graph
names, never with a fresh server.
"""

import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from runic.ogm import Field, Node

from mailarc_core.graph import admin, client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.runtime import DEFAULT_RUNTIME_DIR, RUNTIME_DIR_ENV_VAR
from mailarc_core.graph.server import FalkorDBServer
from mailarc_core.graph.status import read_status

pytestmark = pytest.mark.graph_local

RUNTIME_DIR = Path(DEFAULT_RUNTIME_DIR).resolve()

pytest_skip_reason = f"vendored FalkorDB runtime not present at {RUNTIME_DIR}"


_issued_ports: set[int] = set()


class Probe(Node, labels=["Probe"]):
    """A mapped node, only so a round-trip through runic has something to carry."""

    id: str = Field(primary_key=True)
    subject: str


def _free_port() -> int:
    """Hand out a port nothing is listening on, and never twice.

    `bind(0)` then close is racy here: the port is free when we look but the
    server binds it milliseconds later, so two calls could return the same
    number and one server would silently adopt the other's.
    """
    for candidate in range(6500, 6600):
        if candidate in _issued_ports:
            continue
        with socket.socket() as sock:
            sock.settimeout(0.05)
            if sock.connect_ex(("127.0.0.1", candidate)) == 0:
                continue  # something is already listening
        _issued_ports.add(candidate)
        return candidate
    raise RuntimeError("no free port in 6500-6600 for the test FalkorDB")


@pytest.fixture(scope="session", autouse=True)
def _require_runtime() -> None:
    if not (RUNTIME_DIR / "falkordb.so").is_file():
        pytest.skip(pytest_skip_reason, allow_module_level=True)


@pytest.fixture(scope="session")
def config(tmp_path_factory) -> GraphConfig:
    return GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="127.0.0.1",
        port=_free_port(),
        graph_name="test-graph",
        data_dir=tmp_path_factory.mktemp("falkordb-data"),
        runtime_dir=RUNTIME_DIR,
        startup_timeout=30.0,
    )


@pytest.fixture(scope="session")
def server(config: GraphConfig) -> Iterator[FalkorDBServer]:
    instance = FalkorDBServer(config)
    instance.start()
    try:
        yield instance
    finally:
        # Explicit, deterministic shutdown — never left to interpreter exit.
        instance.stop()


def test_the_server_starts_and_answers(server, config) -> None:
    status = read_status(config)

    assert status.reachable is True, status.error
    assert server.owned is True
    assert status.endpoint == f"127.0.0.1:{config.port}"


def test_the_vendored_module_reports_a_knn_capable_version(server, config) -> None:
    status = read_status(config)

    assert status.falkordb_version is not None
    assert status.vector_knn_supported is True, (
        f"vendored FalkorDB {status.falkordb_version} cannot serve KNN queries"
    )


def test_a_vector_index_query_actually_runs(server, config) -> None:
    """The capability check that matters: a real KNN lookup over vecf32 data.

    FalkorDB builds vector indexes asynchronously, so the query is retried
    briefly rather than fired once into a half-built index.
    """
    from falkordb import FalkorDB

    db = FalkorDB(host=config.host, port=config.port)
    try:
        graph = db.select_graph("knn-probe")
        graph.query(
            "CREATE (:P {name:'near', v: vecf32([1.0,2.0])}), "
            "(:P {name:'far', v: vecf32([9.0,9.0])})"
        )
        graph.query(
            "CREATE VECTOR INDEX FOR (p:P) ON (p.v) "
            "OPTIONS {dimension:2, similarityFunction:'euclidean'}"
        )

        result_set = _await_knn_result(
            graph,
            "CALL db.idx.vector.queryNodes('P','v',1,vecf32([1.0,2.1])) "
            "YIELD node RETURN node.name",
        )

        assert result_set == [["near"]]
    finally:
        db.close()


def _await_knn_result(graph, query: str, timeout: float = 10.0):
    """Poll a KNN query until the async vector index answers."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result_set = graph.ro_query(query).result_set
        except Exception as exc:  # index not built yet
            last_error = exc
        else:
            if result_set:
                return result_set
        time.sleep(0.1)
    if last_error is not None:
        raise last_error
    return []


def test_server_metrics_and_versions_are_populated(server, config) -> None:
    status = read_status(config)

    assert status.redis_version is not None
    assert status.metrics is not None
    assert status.metrics.connected_clients >= 1
    assert status.latency_ms is not None
    assert status.latency_ms >= 0


def test_created_graphs_show_up_in_the_inventory(server, config) -> None:
    driver = client.connect(config)
    try:
        admin.driver_for(driver, "inventory-probe").execute(
            "CREATE (:A)-[:R]->(:B)", {}
        )
    finally:
        client.close(driver)

    status = read_status(config)

    by_name = {graph.name: graph for graph in status.graphs}
    assert "inventory-probe" in by_name
    assert by_name["inventory-probe"].node_count == 2
    assert by_name["inventory-probe"].edge_count == 1


def test_a_runic_session_round_trips_a_mapped_node(server, config) -> None:
    """The point of the refactor: graph data goes in and out through runic.

    Two sessions on purpose — the second cannot be served from the first's
    identity map, so the node really came back off the server.
    """
    with client.session(config) as writing:
        writing.add(Probe(id="p1", subject="hello"))

    with client.session(config) as reading:
        found = reading.get(Probe, "p1")

        assert found is not None
        assert found.subject == "hello"


def test_a_session_leaves_no_connection_behind(server, config) -> None:
    """One leaked connection per session would pile up over a long run."""
    before = read_status(config).metrics
    assert before is not None

    for _ in range(5):
        with client.session(config) as graph_session:
            graph_session.get(Probe, "does-not-exist")

    after = read_status(config).metrics
    assert after is not None
    assert after.connected_clients <= before.connected_clients + 1


def test_start_is_idempotent(server, config) -> None:
    server.start()
    server.start()

    assert read_status(config).reachable is True


def test_a_second_supervisor_adopts_rather_than_competing(server, config) -> None:
    """Two supervisors must not race to bind the same port."""
    adopter = FalkorDBServer(config)
    adopter.start()

    assert adopter.owned is False

    # Stopping the adopter must leave the server the first one owns alone.
    adopter.stop()
    assert read_status(config).reachable is True


def test_stop_is_idempotent_and_leaves_nothing_running(config, tmp_path) -> None:
    disposable = GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="127.0.0.1",
        port=_free_port(),
        graph_name="disposable",
        data_dir=tmp_path / "data",
        runtime_dir=RUNTIME_DIR,
        startup_timeout=30.0,
    )
    instance = FalkorDBServer(disposable)
    instance.start()
    assert read_status(disposable).reachable is True

    instance.stop()
    instance.stop()

    assert read_status(disposable).reachable is False


def test_a_server_that_dies_at_startup_reports_its_output(tmp_path) -> None:
    """Guards the startup-failure path: it must surface the captured server
    output, not blow up inside the error builder.

    Binding to a TEST-NET-1 address (RFC 5737, never assigned to this host)
    makes redis-server fail to create its listening socket and exit.
    """
    unstartable = GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="192.0.2.1",
        port=_free_port(),
        data_dir=tmp_path / "data",
        runtime_dir=RUNTIME_DIR,
        startup_timeout=10.0,
    )
    instance = FalkorDBServer(unstartable)

    try:
        with pytest.raises(RuntimeError) as excinfo:
            instance.start()

        message = str(excinfo.value)
        assert "Server output" in message
        assert "exited during startup" in message
        assert instance.startup_error is not None
    finally:
        instance.stop()


def test_a_missing_runtime_fails_with_an_actionable_message(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(RUNTIME_DIR_ENV_VAR, str(tmp_path / "nothing-here"))
    broken = GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="127.0.0.1",
        port=_free_port(),
        data_dir=tmp_path / "data",
    )

    with pytest.raises(Exception, match="task tauri:vendor"):
        FalkorDBServer(broken).start()
