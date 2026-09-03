"""One vendored FalkorDB for every ``*_local`` test in this directory.

Skipped — per test, through ``endpoint`` — unless `task tauri:vendor` has
produced the runtime, so a checkout without it still runs green. The server
fixture is SESSION scoped and tears itself down explicitly: a function-scoped
one would spawn a redis-server per test and leave every one of them to be
reaped at interpreter exit. Tests isolate themselves with unique graph names,
never with a fresh server.
"""

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.runtime import DEFAULT_RUNTIME_DIR
from mailarc_core.graph.server import FalkorDBServer

RUNTIME_DIR = Path(DEFAULT_RUNTIME_DIR).resolve()

_issued_ports: set[int] = set()


def _free_port() -> int:
    """A port nothing is listening on, and never the same one twice.

    Its own range, well clear of the graph tests' neighbours, so two
    session-scoped servers in one run cannot pick the same number before
    either of them has bound it.
    """
    for candidate in range(6600, 6700):
        if candidate in _issued_ports:
            continue
        with socket.socket() as sock:
            sock.settimeout(0.05)
            if sock.connect_ex(("127.0.0.1", candidate)) == 0:
                continue  # something is already listening
        _issued_ports.add(candidate)
        return candidate
    raise RuntimeError("no free port in 6600-6700 for the test FalkorDB")


@pytest.fixture(scope="session")
def endpoint(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GraphConfig]:
    """One server for the session, torn down explicitly rather than at exit."""
    if not (RUNTIME_DIR / "falkordb.so").is_file():
        pytest.skip(f"vendored FalkorDB runtime not present at {RUNTIME_DIR}")
    config = GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="127.0.0.1",
        port=_free_port(),
        graph_name="archive-probe",
        data_dir=tmp_path_factory.mktemp("archive-falkordb"),
        runtime_dir=RUNTIME_DIR,
        startup_timeout=30.0,
    )
    server = FalkorDBServer(config)
    try:
        # `start` inside the try, not before it: it spawns the process and only
        # then waits for it to answer, so a failure in that wait — or a test
        # runner's timeout landing in it — used to leave a redis-server holding
        # the port with this fixture's `finally` never reached.
        server.start()
        yield config
    finally:
        server.stop()


@pytest.fixture
def config(endpoint: GraphConfig, request: pytest.FixtureRequest) -> GraphConfig:
    """A graph of this test's own, so counts start from an empty one."""
    return endpoint.model_copy(update={"graph_name": f"archive-{request.node.name}"})
