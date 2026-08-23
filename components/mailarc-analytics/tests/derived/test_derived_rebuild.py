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

from mailarc_analytics.derived import rebuild
from mailarc_analytics.queries import catalog

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

REFORMATTED_NODE_DELETE = "MATCH (n:Group)   WITH n LIMIT $batch\nDETACH DELETE n RETURN count(n) AS removed\n"
"""The real statement, laid out differently. Reformatting is not tampering."""


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
        """Whitespace is normalised before the match, so running the formatter
        over the catalogue cannot break the build."""
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
        self, statement: str, label: str
    ) -> None:
        """No unlabelled pattern anywhere: ``MATCH (n)`` would match a message."""
        assert re.search(rf"MATCH \(n:{label}\)", statement)
        assert "MATCH (n)" not in statement

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
