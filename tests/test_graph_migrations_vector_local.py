"""The vector-index migration, run for real against the vendored FalkorDB.

Its ``upgrade`` and ``downgrade`` bodies were executed by nothing at all:
emptying both to ``pass`` left the whole suite green. What existed was a check
on the module's *literals* — that ``DIMENSION`` matches ``SemanticConfig`` and
that ``SIMILARITY`` is ``cosine`` — read off the source text, with nothing
asserting those literals ever reach ``op.create_vector_index``. And every local
semantic test builds its index from a **hand-transcribed** copy of what the
migration is believed to compile to, so a rename inside runic would leave the
whole suite green while the shipped index was built with different parameters.

Per the migration's own docstring, that is the failure that hides: FalkorDB
accepts a vector whose length disagrees with the index, stores it and declines
to index it with no error, no log line and no ``indexingFailures`` count. A job
against a wrong index reports every message embedded and no search finds one.

So this module asks the server. It runs the real revision through runic's real
operations object, reads ``DB.INDEXES()`` back, and asserts the five numbers
the migration chose — then downgrades, checks the index is gone, and upgrades
again, because the docstring claims reversibility and that claim is only worth
what a round trip proves.
"""

import importlib.util
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from runic.migrate.adapters import create_adapter
from runic.migrate.operations import GraphOperations

from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.runtime import DEFAULT_RUNTIME_DIR
from mailarc_core.graph.server import FalkorDBServer

pytestmark = pytest.mark.graph_local

RUNTIME_DIR = Path(DEFAULT_RUNTIME_DIR).resolve()

REVISION = Path(
    "graph_migrations/versions/5f4678dfc5a4_vector_index_on_message_embedding.py"
)
"""Named outright rather than globbed.

``test_semantic_config.py`` resolves its migration with ``sorted(glob)[0]``,
which follows the alphabetically first vector revision rather than the current
head — so the day a second one lands it would keep asserting against the
superseded file. Here the file under test is the point, so it is written down.
"""

FIXTURE_CONFTEST = Path("components/mailarc-analytics/tests/semantic/conftest.py")
"""Where the hand-written copy of this index's DDL lives."""


def _free_port() -> int:
    """A port nothing is listening on, in a range of this module's own.

    A third copy of the same six lines, and deliberately so: the two component
    suites cannot import each other's fixtures, and each needs a range no other
    session-scoped server will pick before it has bound one.
    """
    for candidate in range(6810, 6860):
        with socket.socket() as sock:
            sock.settimeout(0.05)
            if sock.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    raise RuntimeError("no free port in 6810-6860 for the migration test FalkorDB")


def _loaded(path: Path, name: str) -> ModuleType:
    """A file loaded as a module without putting it on the import path.

    A revision is not importable as a package — the directory holds no
    ``__init__`` and the file names start with a digit — so it is loaded the
    way runic loads it: by path.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def endpoint(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GraphConfig]:
    """One vendored server for this module, stopped explicitly at the end."""
    if not (RUNTIME_DIR / "falkordb.so").is_file():
        pytest.skip(f"vendored FalkorDB runtime not present at {RUNTIME_DIR}")
    config = GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="127.0.0.1",
        port=_free_port(),
        graph_name="migration-probe",
        data_dir=tmp_path_factory.mktemp("migration-falkordb"),
        runtime_dir=RUNTIME_DIR,
        startup_timeout=30.0,
    )
    server = FalkorDBServer(config)
    try:
        # Inside the try — a failure during `start` comes after the spawn; see
        # `components/mailarc-core/tests/archive/conftest.py`.
        server.start()
        yield config
    finally:
        server.stop()


@pytest.fixture
def graph(endpoint: GraphConfig, request: pytest.FixtureRequest) -> GraphConfig:
    """A graph of this test's own, so an index from another one cannot leak."""
    return endpoint.model_copy(update={"graph_name": f"migration-{request.node.name}"})


@pytest.fixture
def migration() -> ModuleType:
    """The revision under test, loaded from the file that ships."""
    if not REVISION.is_file():
        pytest.skip("no graph_migrations/ in this checkout")
    return _loaded(REVISION, "revision_5f4678dfc5a4")


@pytest.fixture
def operations(graph: GraphConfig) -> Iterator[GraphOperations]:
    """runic's real ``op``, against this test's own graph.

    The real one and not a recorder: a recorder would prove the migration calls
    a method with some arguments, and what is in doubt is what the *server*
    ends up holding.
    """
    adapter = create_adapter(
        "falkordb", host=graph.host, port=graph.port, graph_name=graph.graph_name
    )
    try:
        yield GraphOperations(adapter)
    finally:
        close = getattr(adapter, "close", None)
        if close is not None:
            close()


def _vector_options(config: GraphConfig) -> dict[str, Any] | None:
    """The live vector index's options on ``Message.embedding``, or ``None``.

    Read straight off ``DB.INDEXES()`` rather than through this project's own
    :func:`~mailarc_analytics.semantic.search.vector_index`, which decodes only
    the two fields it needs. The three tuning numbers are exactly the ones no
    other test looks at.
    """
    with client.session(config) as session:
        result = session.execute(
            "CALL DB.INDEXES() YIELD label, types, options "
            "RETURN label, types, options",
            {},
        )
    for label, types, options in result.rows:
        if label != "Message":
            continue
        if "VECTOR" not in tuple(types.get("embedding") or ()):
            continue
        settings = options.get("embedding", options)
        return dict(settings) if isinstance(settings, dict) else {}
    return None


class TestTheIndexTheMigrationReallyBuilds:
    def test_the_five_numbers_reach_the_server(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        """Every literal the revision chose, read back off the running store.

        ``efRuntime`` is the one that matters most and the one nothing else
        checks: there is no query-time override — ``db.idx.vector.queryNodes``
        takes four arguments and rejects an options map — so runic's default of
        10 would ship a 14 % recall@10 index that answers plausibly and wrongly.
        """
        migration.upgrade(operations)

        options = _vector_options(graph)

        assert options is not None, "the migration built no vector index"
        assert options["dimension"] == migration.DIMENSION
        assert options["similarityFunction"] == migration.SIMILARITY
        assert options["M"] == migration.M
        assert options["efConstruction"] == migration.EF_CONSTRUCTION
        assert options["efRuntime"] == migration.EF_RUNTIME

    def test_a_downgrade_really_removes_it(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        migration.upgrade(operations)

        migration.downgrade(operations)

        assert _vector_options(graph) is None

    def test_it_can_be_built_again_from_the_vectors_already_stored(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        """The reversibility the docstring claims. FalkorDB refuses a second
        ``CREATE VECTOR INDEX`` on an indexed attribute, so this only holds
        because the downgrade above really dropped it — which is the same thing
        said twice, and worth saying twice: an upgrade that silently failed
        here would leave an archive whose searches find nothing."""
        with client.session(graph) as session:
            session.execute(
                "CREATE (m:Message {id: 'm1'}) "
                "SET m.embedding = vecf32($v), m.embedding_model = 'probe'",
                {"v": [0.0] * migration.DIMENSION},
            )
        migration.upgrade(operations)
        migration.downgrade(operations)

        migration.upgrade(operations)

        assert _vector_options(graph) is not None
        with client.session(graph) as session:
            found = session.execute(
                "CALL db.idx.vector.queryNodes('Message', 'embedding', 1, vecf32($v)) "
                "YIELD node RETURN node.id",
                {"v": [0.0] * migration.DIMENSION},
            )
        assert [row[0] for row in found.rows] == ["m1"], (
            "the vector already on the node is indexed again, not re-embedded"
        )


def test_the_fixture_ddl_is_what_the_migration_compiles_to(
    migration: ModuleType,
) -> None:
    """The hand-written copy and the real thing, held against each other.

    Every ``*_local`` semantic test builds its index from ``VECTOR_INDEX`` in
    ``components/mailarc-analytics/tests/semantic/conftest.py`` — a transcription
    of what this revision is *believed* to produce. If runic renames an option
    or reorders the map, that fixture keeps passing while the shipped index is
    built differently, and nothing anywhere would notice.

    Needs no server: the adapter's compiler is asked what it would send.
    """
    if not FIXTURE_CONFTEST.is_file():
        pytest.skip("no mailarc-analytics test fixtures in this checkout")
    fixture = _loaded(FIXTURE_CONFTEST, "semantic_fixture_conftest")
    sent: list[str] = []

    class RecordingDriver:
        def execute(self, statement: str, params: dict[str, Any]) -> None:
            del params
            sent.append(statement)

    from runic.migrate.adapters.falkordb import FalkorDBAdapter

    adapter = FalkorDBAdapter.__new__(FalkorDBAdapter)
    # The adapter's driver is the seam: everything above it is the compiler
    # under test and everything below it is a socket. Assigned rather than
    # injected because the constructor connects, and this test must not.
    adapter._driver = cast(Any, RecordingDriver())  # noqa: SLF001
    adapter.create_vector_index(
        migration.LABEL,
        migration.PROPERTY,
        fixture.TEST_DIMENSION,
        migration.SIMILARITY,
        m=migration.M,
        ef_construction=migration.EF_CONSTRUCTION,
        ef_runtime=migration.EF_RUNTIME,
    )

    assert sent == [fixture.VECTOR_INDEX % fixture.TEST_DIMENSION]
