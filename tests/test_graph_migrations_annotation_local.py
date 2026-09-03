"""The annotation-layer migration, run for real against the vendored FalkorDB.

Modelled on ``test_graph_migrations_vector_local.py`` and for its reason: a
revision's body is executed by nothing else in the suite, so emptying
``upgrade`` to ``pass`` would leave everything green while the shipped archive
had no constraint on ``Tag.id`` and no index behind either derived score.

Here the silent failure is not a wrong parameter but a missing guarantee.
``TagRepository.create`` looks a key up before it writes, and a lookup is not a
constraint: two sessions can both find nothing and both write, and the result
is one project's mail split across two ``Tag`` nodes that no listing can tell
apart. So this module does not check that the migration *calls* something — it
runs the revision, then asks the server to reject a duplicate.

The downgrade is checked as carefully as the upgrade, because it carries a trap
of its own: ``create_constraint`` builds the range index itself, ``GRAPH.CONSTRAINT
DROP`` does not remove it, and dropping it first is refused with "Index supports
constraint". A downgrade that got that order or that omission wrong would leave
an index behind and the next upgrade would fail on it.
"""

import importlib.util
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from runic.migrate.adapters import create_adapter
from runic.migrate.operations import GraphOperations

from mailarc_core.archive.model import Address, Message
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.runtime import DEFAULT_RUNTIME_DIR
from mailarc_core.graph.server import FalkorDBServer

pytestmark = pytest.mark.graph_local

RUNTIME_DIR = Path(DEFAULT_RUNTIME_DIR).resolve()

REVISION = Path("graph_migrations/versions/3824f164c0a6_annotation_layer.py")
"""Named outright rather than globbed, for the reason the vector test gives:
the file under test is the point, so it is written down."""


def _free_port() -> int:
    """A port nothing is listening on, in a range of this module's own.

    A fourth copy of the same six lines, and deliberately so: the component
    suites cannot import each other's fixtures, and each needs a range no other
    session-scoped server will pick before it has bound one.
    """
    for candidate in range(6860, 6910):
        with socket.socket() as sock:
            sock.settimeout(0.05)
            if sock.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    raise RuntimeError("no free port in 6860-6910 for the migration test FalkorDB")


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
        graph_name="annotation-probe",
        data_dir=tmp_path_factory.mktemp("annotation-falkordb"),
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
    return endpoint.model_copy(update={"graph_name": f"annotation-{request.node.name}"})


@pytest.fixture
def migration() -> ModuleType:
    """The revision under test, loaded from the file that ships."""
    if not REVISION.is_file():
        pytest.skip("no graph_migrations/ in this checkout")
    return _loaded(REVISION, "revision_3824f164c0a6")


@pytest.fixture
def operations(graph: GraphConfig) -> Iterator[GraphOperations]:
    """runic's real ``op``, against this test's own graph.

    The real one and not a recorder: a recorder proves the migration called a
    method, and what is in doubt is what the *server* ends up holding.
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


def _indexes(config: GraphConfig) -> set[tuple[str, str]]:
    """Every ``(label, property)`` the live graph has an index on."""
    with client.session(config) as session:
        result = session.execute(
            "CALL DB.INDEXES() YIELD label, properties RETURN label, properties", {}
        )
    return {
        (str(label), str(prop))
        for label, properties in result.rows
        for prop in properties
    }


def _constraints(config: GraphConfig) -> list[tuple[str, str, tuple[str, ...], str]]:
    """``(type, label, properties, status)`` for every live constraint."""
    with client.session(config) as session:
        result = session.execute("CALL db.constraints()", {})
    return [
        (str(row[0]), str(row[1]), tuple(str(one) for one in row[2]), str(row[4]))
        for row in result.rows
    ]


def _rows(config: GraphConfig, cypher: str) -> list[Any]:
    with client.session(config) as session:
        return list(session.execute(cypher, {}).rows)


class TestWhatTheUpgradeReallyBuilds:
    def test_the_tag_key_is_constrained_and_operational(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        """``OPERATIONAL`` and not merely present: FalkorDB reports a constraint
        while it is still being built, and one stuck at ``PENDING`` enforces
        nothing."""
        migration.upgrade(operations)

        assert _constraints(graph) == [
            ("UNIQUE", migration.TAG_LABEL, (migration.TAG_KEY,), "OPERATIONAL")
        ]

    def test_the_constraint_really_refuses_a_second_tag(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        """The guarantee the repository's lookup cannot give. Written through
        raw Cypher rather than through ``TagRepository``, whose own check would
        answer first and prove nothing about the graph."""
        migration.upgrade(operations)
        _rows(graph, "CREATE (t:Tag {id: 'tag:nord-42', name: 'NORD-42'})")

        with pytest.raises(Exception, match="unique constraint violation"):
            _rows(graph, "CREATE (t:Tag {id: 'tag:nord-42', name: 'again'})")

        assert _rows(graph, "MATCH (t:Tag) RETURN count(t)") == [[1]]

    def test_both_derived_scores_are_indexed(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        """Phase 2 orders by both, and either is a full sort of every node
        wearing the label without an index."""
        migration.upgrade(operations)

        assert set(migration.SCORE_INDEXES) <= _indexes(graph)

    def test_the_constraint_brings_its_own_index(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        """Which is why the upgrade asks for no range index on ``Tag.id``:
        FalkorDB rejects a second ``CREATE INDEX`` on an indexed attribute, so
        asking for both fails the migration."""
        migration.upgrade(operations)

        assert (migration.TAG_LABEL, migration.TAG_KEY) in _indexes(graph)


class TestTheDowngrade:
    def test_it_leaves_nothing_behind(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        migration.upgrade(operations)

        migration.downgrade(operations)

        assert _constraints(graph) == []
        assert _indexes(graph) == set()

    def test_dropping_the_constraint_alone_would_leave_the_index(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        """The omission the downgrade's explicit ``drop_range_index`` exists
        for. ``GRAPH.CONSTRAINT DROP`` removes the constraint and not the index
        it created, so a downgrade without that line leaves ``Tag.id`` indexed —
        and the next upgrade fails on "already indexed"."""
        migration.upgrade(operations)

        operations.drop_constraint(
            "UNIQUE", "NODE", migration.TAG_LABEL, [migration.TAG_KEY]
        )

        assert (migration.TAG_LABEL, migration.TAG_KEY) in _indexes(graph)

    def test_the_round_trip_can_be_made_twice(
        self, migration: ModuleType, operations: GraphOperations, graph: GraphConfig
    ) -> None:
        """The reversibility the docstring claims, which is only worth what a
        round trip proves — and the check that the downgrade really cleared the
        way, because a second ``CREATE INDEX`` on an indexed attribute is
        refused."""
        migration.upgrade(operations)
        migration.downgrade(operations)

        migration.upgrade(operations)

        assert _constraints(graph) == [
            ("UNIQUE", migration.TAG_LABEL, (migration.TAG_KEY,), "OPERATIONAL")
        ]
        assert set(migration.SCORE_INDEXES) <= _indexes(graph)


def test_the_indexed_properties_are_the_ones_the_models_declare(
    migration: ModuleType,
) -> None:
    """Migration and writer have to agree about the names, and nothing else
    holds them together: an index on ``Message.importance`` is silently useless
    if the writer sets ``Message.score``.

    Read off the model's own field list rather than a literal, so a rename in
    ``archive/model.py`` fails here instead of showing up as a slow page.
    """
    declared = {
        ("Message", one.name)
        for one in Message._fields  # noqa: SLF001
    } | {("Address", one.name) for one in Address._fields}  # noqa: SLF001

    assert set(migration.SCORE_INDEXES) <= declared
