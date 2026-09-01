"""The guard that makes ``rebuild-derived`` structurally unable to lose data.

Everything else in this package can be wrong and cost one run. A delete cannot:
``DETACH DELETE`` over the wrong pattern takes the archive with it, and there is
no second copy of a graph the user imported over a week. So
:mod:`~mailarc_analytics.derived.rebuild` matches its four delete statements
against two exact shapes **at import time**, and this file is what proves the
match is real rather than decorative — by swapping each statement for one that
would destroy something and showing the module then refuses to load.

The two shapes differ in a way worth stating. A derived *node* is removed with
``DETACH DELETE``, which is correct precisely because the label is derived: the
node goes and the edges incident to it go with it, so ``ADDRESSED_GROUP``,
``ABOUT`` and ``INSTANCE_OF`` need no statement of their own and no ``Message``
is even matched. ``CO_ADDRESSED`` is the one derived thing that lives *between*
two ground-truth nodes, so it is deleted as a relationship variable with both
endpoints anonymous — detaching there would take two ``Address`` nodes down and
every ``SENT_TO`` in the archive with them.
"""

import importlib
import re
from collections.abc import Iterator
from types import ModuleType

import pytest
from runic.ogm import alias, count, param, select

from mailarc_analytics.derived import rebuild
from mailarc_analytics.derived.model import CoAddressed
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import Statement
from mailarc_core.archive.model import Address, Message

DESTRUCTIVE_NODE_DELETE = """\
MATCH (n:Message)
WITH n LIMIT $batch
DETACH DELETE n
RETURN count(n) AS removed
"""
"""What a careless edit looks like: the same shape over ground truth."""

DESTRUCTIVE_EDGE_DELETE = """\
MATCH (:Address)-[r:CO_ADDRESSED]->(:Address)
WITH r LIMIT $batch
DETACH DELETE r
RETURN count(r) AS removed
"""
"""``DETACH`` on the edge statement — the one that would take the addresses."""

REFORMATTED_NODE_DELETE = (
    "MATCH (n:Group)   WITH n LIMIT $batch\n"
    "DETACH DELETE n RETURN count(n) AS `removed`\n"
)
"""The real statement, laid out differently. Reformatting is not tampering.

Both differences the guard has to see past are in here: the whitespace, and the
backticks runic puts around every identifier it emits so that a model may
declare a field named after a Cypher keyword. Neither changes what the store
would run, and a guard that read either as tampering would refuse to import
over the catalogue's own compiled statements.
"""

DESTRUCTIVE_NODE_STATEMENT = (
    select(Message)
    .with_("n", limit=param("batch"))
    .delete(detach=True)
    .returning(count("n").as_("removed"))
)
"""The same careless edit, written the way the catalogue is written now.

A catalogue statement is a query-builder object, so the realistic way to lose
an archive is no longer a mistyped string — it is one word changed in a
``select()``. The guard compiles what it is given and reads the Cypher, so it
catches this exactly as it catches :data:`DESTRUCTIVE_NODE_DELETE`; that it
does is asserted rather than assumed.
"""

_EDGE = alias(CoAddressed, "r")

DESTRUCTIVE_EDGE_STATEMENT = (
    select(alias(Address, "a"))
    .traverse(Address.co_addressed, to="b", edge=_EDGE)
    .with_(_EDGE, limit=param("batch"))
    .delete(_EDGE, detach=True)
    .returning(count("r").as_("removed"))
)
"""``DETACH`` on the edge statement, in the form an edit would now take.

The one keyword that separates this from :data:`catalog.DELETE_CO_ADDRESSED`
is ``detach=True``, and ``delete()`` accepts it without complaint — it is the
right flag three statements further up, where the node deletions want it. Here
it takes both ``Address`` endpoints and every ``SENT_TO`` and ``COPIED_TO``
edge in the archive with them. Compiles to ``… WITH r LIMIT $batch DETACH
DELETE r RETURN count(r) AS removed``.
"""

ENDPOINT_DELETING_STATEMENT = (
    select(alias(Address, "a"))
    .traverse(Address.co_addressed, to="b", edge=_EDGE)
    .with_("a", limit=param("batch"))
    .delete("a", detach=True)
    .returning(count("a").as_("removed"))
)
"""The other way round: the right pattern, the wrong variable deleted.

``DELETE r`` and ``DELETE a`` are one character apart and the second empties
the archive's address book. This is the case the guard's regex is exact for —
matching "some delete over CO_ADDRESSED" would let it through.
"""


def _cypher(statement: Statement) -> str:
    """What the store will actually run.

    The same reading the guard takes: a statement is a
    :class:`~runic.ogm.QueryBuilder` and the thing that could destroy an
    archive is the text it compiles to, not the object.
    """
    return statement if isinstance(statement, str) else statement.build()[0]


@pytest.fixture
def reloadable() -> Iterator[ModuleType]:
    """Reload the module after the test, whatever the test did to it.

    The guard runs at import time, so the only way to exercise it is to reload
    with a swapped statement — and the only way to leave the suite intact is to
    reload again with the real one.
    """
    try:
        yield rebuild
    finally:
        importlib.reload(rebuild)


class TestTheImportTimeGuard:
    """Each of the four statements, swapped for one that would do damage."""

    @pytest.mark.parametrize(
        "name", ["DELETE_GROUPS", "DELETE_TOPICS", "DELETE_TEMPLATES"]
    )
    def test_a_node_delete_over_a_ground_truth_label_refuses_to_import(
        self, reloadable: ModuleType, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        """The failure happens before a session is ever opened."""
        monkeypatch.setattr(catalog, name, DESTRUCTIVE_NODE_DELETE)

        with pytest.raises(ValueError, match="unknown shape"):
            importlib.reload(reloadable)

    def test_detaching_the_co_addressed_edge_refuses_to_import(
        self, reloadable: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``DETACH DELETE r`` deletes the relationship *and its endpoints*.

        Both endpoints here are ``Address`` nodes, so the statement that reads
        almost identically to the safe one would empty the archive's address
        book and every edge hanging off it.
        """
        monkeypatch.setattr(catalog, "DELETE_CO_ADDRESSED", DESTRUCTIVE_EDGE_DELETE)

        with pytest.raises(ValueError, match="unknown shape"):
            importlib.reload(reloadable)

    @pytest.mark.parametrize(
        ("label", "statement"),
        [
            ("detaching the edge", DESTRUCTIVE_EDGE_STATEMENT),
            ("deleting an endpoint", ENDPOINT_DELETING_STATEMENT),
        ],
    )
    def test_a_builder_edge_delete_that_would_take_the_addresses_refuses(
        self,
        reloadable: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        label: str,
        statement: Statement,
    ) -> None:
        """The edge guard, against the two edits the builder now makes easy.

        :data:`DESTRUCTIVE_EDGE_DELETE` proves the regex on a *string*, which
        is no longer the shape a careless edit takes. ``delete(edge,
        detach=True)`` and ``delete("a", detach=True)`` both compile, both
        type-check, and either one would delete every ``Address`` in the
        archive along with every ``SENT_TO`` hanging off it. The guard reads
        the compiled Cypher, so it catches both — asserted here rather than
        inferred from the string case.
        """
        monkeypatch.setattr(catalog, "DELETE_CO_ADDRESSED", statement)

        with pytest.raises(ValueError, match="unknown shape"):
            importlib.reload(reloadable)

    def test_a_builder_statement_over_ground_truth_refuses_to_import(
        self, reloadable: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same careless edit in the form the catalogue now takes.

        ``select(Message)`` instead of ``select(Group)`` is one word, compiles
        cleanly, type-checks, and would delete every message in the archive.
        The guard reads the compiled Cypher rather than the object, which is
        the only reading that can tell the two apart.
        """
        monkeypatch.setattr(catalog, "DELETE_GROUPS", DESTRUCTIVE_NODE_STATEMENT)

        with pytest.raises(ValueError, match="unknown shape"):
            importlib.reload(reloadable)

    def test_an_unbatched_delete_refuses_to_import(
        self, reloadable: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FalkorDB has no ``CALL … IN TRANSACTIONS``, so one unbounded delete
        over a large archive is a single long stall on a store the UI reads."""
        monkeypatch.setattr(
            catalog,
            "DELETE_GROUPS",
            "MATCH (n:Group) DETACH DELETE n RETURN count(n) AS removed",
        )

        with pytest.raises(ValueError, match="unknown shape"):
            importlib.reload(reloadable)

    def test_reformatting_the_statement_is_not_tampering(
        self, reloadable: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Layout is normalised before the match, so neither running the
        formatter over the catalogue nor the compiler's own backticks can
        break the build."""
        monkeypatch.setattr(catalog, "DELETE_GROUPS", REFORMATTED_NODE_DELETE)

        importlib.reload(reloadable)

        assert REFORMATTED_NODE_DELETE in reloadable._NODE_DELETIONS


class TestWhatTheModuleActuallyHolds:
    """Three derived labels and one derived edge type, and no others."""

    def test_the_three_node_deletions_are_the_three_derived_labels(self) -> None:
        assert rebuild._NODE_DELETIONS == (
            catalog.DELETE_GROUPS,
            catalog.DELETE_TOPICS,
            catalog.DELETE_TEMPLATES,
        )

    def test_the_one_edge_deletion_is_co_addressed(self) -> None:
        assert rebuild._EDGE_DELETIONS == (catalog.DELETE_CO_ADDRESSED,)

    @pytest.mark.parametrize(
        ("statement", "label"),
        [
            (catalog.DELETE_GROUPS, "Group"),
            (catalog.DELETE_TOPICS, "Topic"),
            (catalog.DELETE_TEMPLATES, "Template"),
        ],
    )
    def test_every_node_deletion_names_its_own_derived_label(
        self, statement: Statement, label: str
    ) -> None:
        """No unlabelled pattern anywhere: ``MATCH (n)`` would match a message.

        Read off the compiled Cypher, because that is what the store runs and
        what the import-time guard checks. The claim is unchanged: the label is
        in the pattern and the pattern is never bare.
        """
        cypher = _cypher(statement)

        assert re.search(rf"MATCH \(n:{label}\)", cypher)
        assert "MATCH (n)" not in cypher

    def test_the_module_composes_no_cypher_of_its_own(self) -> None:
        """The rebuild takes no statement, no label and no pattern from its
        caller, so there is nothing for a caller to redirect.

        ``extra_edges`` is on the list and is not an exception to it: it is a
        sequence of :class:`~mailarc_analytics.derived.model.SimilarityEdge`
        values — two message ids, a method and a weight — that ends up in
        ``build_topics``' union-find and never in a statement. Named here so
        that a *sixth* parameter is a visible edit rather than a silent one.
        """
        parameters = rebuild.rebuild_derived.__annotations__

        assert set(parameters) == {
            "session",
            "config",
            "on_progress",
            "extra_edges",
            "return",
        }
