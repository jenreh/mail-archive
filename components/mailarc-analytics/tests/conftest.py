"""One vendored FalkorDB for the component, and the corpus written into it.

Two things every ``*_local`` test needs and neither of them belongs in a test
file: a server, and an archive to read. The pure tests decide whether the three
analyses are *right*; the fixtures here exist for the questions only a graph can
answer — whether the reader gets the same facts back out, whether a ``MERGE``
really is idempotent, and whether a rebuild leaves the ground truth exactly as
it found it.

The corpus is archived through
:class:`~mailarc_core.archive.writer.MessageArchiver` rather than through
hand-written Cypher, because the point of a round trip is that the producer and
the consumer are both the real ones. How that is done lives in
``planted_graph.py``, which a test module can import by name — ``conftest`` is
pytest's convention rather than a path, and in a repository-wide run the first
one imported owns the name.

Skipped — per test, through ``endpoint`` — unless `task tauri:vendor` has
produced the runtime, so a checkout without it still runs green. The server
fixture is SESSION scoped and tears itself down explicitly: a function-scoped
one would spawn a redis-server per test and leave every one of them to be
reaped at interpreter exit, which turns a suite that runs in seconds into one
that hangs for minutes after the last assertion. Tests isolate themselves with
unique graph names, never with a fresh server.

A near-copy of ``components/mailarc-core/tests/archive/conftest.py``, and
deliberately a copy: a component may not import another component's tests, and
the sixty lines are cheaper than the coupling. The port range is the one thing
that differs — the two servers must not agree on a number before either has
bound it.

It all sits at the component's test root because two directories need it:
``derived/`` proves the analyses against a real graph, ``queries/`` proves that
every catalogue statement actually compiles on the backend it was written for.
"""

import socket
from collections.abc import Iterator
from pathlib import Path

import corpus
import planted_graph
import pytest

from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.runtime import DEFAULT_RUNTIME_DIR
from mailarc_core.graph.server import FalkorDBServer

RUNTIME_DIR = Path(DEFAULT_RUNTIME_DIR).resolve()

_issued_ports: set[int] = set()


def _free_port() -> int:
    """A port nothing is listening on, and never the same one twice.

    Its own range, well clear of the core component's, so a run that starts
    both session-scoped servers cannot have them pick the same number before
    either of them has bound it.
    """
    for candidate in range(6700, 6800):
        if candidate in _issued_ports:
            continue
        with socket.socket() as sock:
            sock.settimeout(0.05)
            if sock.connect_ex(("127.0.0.1", candidate)) == 0:
                continue  # something is already listening
        _issued_ports.add(candidate)
        return candidate
    raise RuntimeError("no free port in 6700-6800 for the test FalkorDB")


@pytest.fixture(scope="session")
def endpoint(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GraphConfig]:
    """One server for the session, torn down explicitly rather than at exit."""
    if not (RUNTIME_DIR / "falkordb.so").is_file():
        pytest.skip(f"vendored FalkorDB runtime not present at {RUNTIME_DIR}")
    config = GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="127.0.0.1",
        port=_free_port(),
        graph_name="analytics-probe",
        data_dir=tmp_path_factory.mktemp("analytics-falkordb"),
        runtime_dir=RUNTIME_DIR,
        startup_timeout=30.0,
    )
    server = FalkorDBServer(config)
    server.start()
    try:
        yield config
    finally:
        server.stop()


@pytest.fixture
def config(endpoint: GraphConfig, request: pytest.FixtureRequest) -> GraphConfig:
    """A graph of this test's own, so counts start from an empty one."""
    return endpoint.model_copy(update={"graph_name": f"analytics-{request.node.name}"})


@pytest.fixture
def archived(config: GraphConfig) -> GraphConfig:
    """A graph holding the whole planted corpus and nothing else.

    No teardown: ``config`` hands out a graph name of this test's own, so the
    graph is thrown away with the session's server rather than emptied between
    tests.
    """
    planted_graph.archive(config, corpus.planted_corpus())
    return config
