"""The guard that makes ``rebuild-derived`` structurally unable to lose data.

Everything else in this package can be wrong and cost one run. A delete cannot:
``DETACH DELETE`` over the wrong pattern takes the archive with it, and there is
no second copy of a graph the user imported over a week. So
:mod:`~mailarc_analytics.derived.rebuild` matches its eight destructive
statements against four exact shapes **at import time**, and this file is what
proves the match is real rather than decorative — by swapping each statement
for one that would destroy something and showing the module then refuses to
load.

The four shapes differ in ways worth stating. A derived *node* is removed with
``DETACH DELETE``, which is correct precisely because the label is derived: the
node goes and the edges incident to it go with it, so ``ADDRESSED_GROUP``,
``ABOUT``, ``INSTANCE_OF``, ``MEMBER_OF`` and ``IN_CIRCLE`` need no statement of
their own and no ``Message`` is even matched. ``CO_ADDRESSED`` is one of the two
derived things that live *between* two ground-truth nodes, so it is deleted as a
relationship variable with both endpoints named and kept — detaching there would
take two ``Address`` nodes down and every ``SENT_TO`` in the archive with them.

``SUGGESTED`` is the other, and it is the more dangerous of the two: its
endpoints are a ``Message`` and a **``Tag``** — the annotation layer, the one
part of this graph that holds what a person decided and that no rebuild may
touch. So it gets a shape of its own, ``DELETE r`` with the tag and the message
both kept, and the whole point of this file is that **no delete regex in the
module can reach the label ``Tag``**: a node deletion over it, and an edge
deletion that deletes the tag variable instead of the edge one, both refuse to
import.

The fourth shape has no delete in it at all. The two ``CLEAR_*`` statements
reset the properties this phase writes onto ``Message`` and ``Address``, which
makes them the only statements in the rebuild that name a ground-truth label
outside a read — safe because ``SET … = NULL`` removes a property and nothing
else, and guarded for exactly that reason: the delete shapes above are never
applied to them, so a reset rewritten as a deletion would have nothing looking
at it.
"""

import importlib
import re
from collections.abc import Iterator
from types import ModuleType

import pytest
from runic.ogm import alias, count, param, select

from mailarc_analytics.derived import rebuild
from mailarc_analytics.derived.model import CoAddressed, Suggested
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import Statement
from mailarc_core.archive.model import Address, Message, Tag

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

TAG_DELETING_STATEMENT = (
    select(Tag)
    .with_("n", limit=param("batch"))
    .delete(detach=True)
    .returning(count("n").as_("removed"))
)
"""A node delete over the **annotation layer**, in the node shape exactly.

The one label in this graph that is neither ground truth nor derived: a ``Tag``
is what a person decided a set of messages is called, it survives every rebuild
and it survives an account clear-out. ``select(Tag)`` beside ``select(Group)``
is one word, and this statement would delete every project name in the archive
along with every ``TAGGED`` membership behind it — the one loss in this
repository that no re-import could repair, because nothing outside the graph
ever held it.

The node guard names the four derived labels and only those, so this refuses.
"""

_SUGGESTED = alias(Suggested, "r")

DETACHING_SUGGESTION_STATEMENT = (
    select(alias(Tag, "t"))
    .traverse(Tag.suggested, to="m", edge=_SUGGESTED)
    .with_(_SUGGESTED, limit=param("batch"))
    .delete(_SUGGESTED, detach=True)
    .returning(count("r").as_("removed"))
)
"""``DETACH`` on the suggestion edge — the tag and the message go with it.

:data:`DESTRUCTIVE_EDGE_STATEMENT`'s mistake on the more dangerous of the two
edges. ``SUGGESTED`` runs from a ``Message`` to a ``Tag``, so detaching here
deletes both endpoints: an archived message with every edge the import gave it,
and a tag a human named.
"""

TAG_DELETING_EDGE_STATEMENT = (
    select(alias(Tag, "t"))
    .traverse(Tag.suggested, to="m", edge=_SUGGESTED)
    .with_("t", limit=param("batch"))
    .delete("t", detach=True)
    .returning(count("t").as_("removed"))
)
"""The right pattern, the wrong variable: ``DELETE t`` takes the tag itself.

``DELETE r`` and ``DELETE t`` are one character apart, and the second empties
the annotation layer of every tag an analysis had anything to suggest for —
which is every tag anybody uses.
"""

DELETING_PROPERTY_CLEAR = """\
MATCH (m:Message)
WHERE (m.importance IS NOT NULL) OR (m.importance_version IS NOT NULL)
WITH m LIMIT $batch
DETACH DELETE m
RETURN count(m) AS cleared
"""
"""A reset turned into a deletion — the mistake the clear guard exists for.

The two ``CLEAR_*`` statements are the only ones in the rebuild that name
``Message`` and ``Address`` outside a read, and they are safe because
``SET … = NULL`` removes a property and nothing else. Written like this instead,
the statement reads plausibly, matches on exactly the messages a rebuild is
about to rescore, and deletes them. Neither delete guard would see it, because
it is not one of the statements they are applied to.
"""

OVERREACHING_PROPERTY_CLEAR = """\
MATCH (m:Message)
WHERE (m.importance IS NOT NULL) OR (m.importance_version IS NOT NULL)
SET m.importance = NULL, m.importance_reasons = NULL, m.subject = NULL
RETURN count(m) AS cleared
"""
"""The right verb over a property this phase does not own.

``importance_version`` swapped for ``subject``: still a ``SET … = NULL``, still
no delete, and it would null a field the *import* wrote and nothing recomputes.
Which is why the shape pins the property names rather than merely the verb.
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
    """Each of the eight statements, swapped for one that would do damage."""

    @pytest.mark.parametrize(
        "name",
        ["DELETE_GROUPS", "DELETE_TOPICS", "DELETE_TEMPLATES", "DELETE_COMMUNITIES"],
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

    @pytest.mark.parametrize("name", ["CLEAR_IMPORTANCE", "CLEAR_ADDRESS_RANKS"])
    @pytest.mark.parametrize(
        "statement",
        [
            pytest.param(DELETING_PROPERTY_CLEAR, id="deleting the node"),
            pytest.param(
                OVERREACHING_PROPERTY_CLEAR, id="nulling a property the import wrote"
            ),
        ],
    )
    def test_a_property_clear_that_does_more_than_reset_refuses_to_import(
        self,
        reloadable: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        statement: Statement,
    ) -> None:
        """The fourth shape, and the one with no delete in it to give it away.

        These two statements sit on *ground-truth* nodes, so they are the only
        place in the rebuild where a wrong word reaches something no
        recomputation can restore — and the delete guards above never look at
        them. A reset that deletes the node, and a reset that nulls a field the
        import wrote, both have to be refused before a session is opened.
        """
        monkeypatch.setattr(catalog, name, statement)

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

    def test_a_node_delete_over_the_annotation_layer_refuses_to_import(
        self, reloadable: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Tag`` is not a derived label and no delete regex may reach it.

        The annotation layer is the one thing in this graph a rebuild must
        leave exactly where it is: it holds what a person decided, it is what
        survives when a topic id is recomputed into a different string, and
        nothing outside the graph ever held a copy of it. A node deletion over
        it is one word away from a legal one — ``select(Tag)`` beside
        ``select(Community)`` — so the guard names the four derived labels and
        refuses everything else.
        """
        monkeypatch.setattr(catalog, "DELETE_COMMUNITIES", TAG_DELETING_STATEMENT)

        with pytest.raises(ValueError, match="unknown shape"):
            importlib.reload(reloadable)

    @pytest.mark.parametrize(
        ("label", "statement"),
        [
            ("detaching the edge", DETACHING_SUGGESTION_STATEMENT),
            ("deleting the tag", TAG_DELETING_EDGE_STATEMENT),
        ],
    )
    def test_a_suggestion_delete_that_would_take_a_tag_refuses_to_import(
        self,
        reloadable: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        label: str,
        statement: Statement,
    ) -> None:
        """The suggestion edge's own shape, against the two edits it invites.

        ``SUGGESTED`` runs between a ``Message`` and a ``Tag``, so both a
        ``DETACH`` and a delete of the root variable take ground truth or the
        annotation layer with them. The shape this guard holds is ``DELETE r``
        with both endpoints named and kept, and nothing else compiles past it.
        """
        monkeypatch.setattr(catalog, "DELETE_SUGGESTED", statement)

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
    """Four derived labels and two derived edge types, and no others."""

    def test_the_four_node_deletions_are_the_four_derived_labels(self) -> None:
        assert rebuild._NODE_DELETIONS == (
            catalog.DELETE_GROUPS,
            catalog.DELETE_TOPICS,
            catalog.DELETE_TEMPLATES,
            catalog.DELETE_COMMUNITIES,
        )

    def test_the_two_edge_deletions_are_the_two_edges_between_kept_nodes(
        self,
    ) -> None:
        """The only two derived things that outlive their endpoints.

        Every other derived edge — ``ADDRESSED_GROUP``, ``ABOUT``,
        ``INSTANCE_OF``, ``MEMBER_OF``, ``IN_CIRCLE`` — hangs off a derived
        node and leaves with it, which is exactly why those need no statement
        and why a fifth entry here would be a sign that something is being
        deleted the hard way.
        """
        assert rebuild._EDGE_DELETIONS == (
            catalog.DELETE_CO_ADDRESSED,
            catalog.DELETE_SUGGESTED,
        )

    def test_the_two_property_clears_are_the_two_reset_statements(self) -> None:
        """A third one would mean this phase writes onto a ground-truth node
        somewhere the ``CLEAR_*`` pair does not reach — which is how a property
        survives the rebuild that was supposed to recompute it."""
        assert rebuild._PROPERTY_CLEARS == (
            catalog.CLEAR_IMPORTANCE,
            catalog.CLEAR_ADDRESS_RANKS,
        )

    def test_the_suggestion_delete_deletes_the_edge_and_nothing_else(self) -> None:
        """Read off the compiled Cypher, because that is what the store runs.

        ``DELETE r`` and never ``DETACH``, and neither endpoint variable
        deleted: the tag stays because a person named it and the message stays
        because the import wrote it.
        """
        cypher = rebuild._normalised(_cypher(catalog.DELETE_SUGGESTED))

        assert cypher.endswith("DELETE r RETURN count(r) AS removed")
        assert "DETACH" not in cypher
        assert "DELETE t" not in cypher

    def test_no_delete_statement_can_reach_the_annotation_layer(self) -> None:
        """``Tag`` appears in exactly one delete, as a node that is kept.

        The strong form of what the two guards buy, stated once over all six
        statements rather than inferred from the shapes: the only delete that
        names ``Tag`` at all is the suggestion edge's, which matches the tag in
        order to walk off it and deletes the relationship variable.
        """
        naming = [
            rebuild._normalised(_cypher(statement))
            for statement in rebuild._NODE_DELETIONS + rebuild._EDGE_DELETIONS
            if "Tag" in _cypher(statement)
        ]

        assert naming == [rebuild._normalised(_cypher(catalog.DELETE_SUGGESTED))]

    @pytest.mark.parametrize(
        ("statement", "label"),
        [
            (catalog.DELETE_GROUPS, "Group"),
            (catalog.DELETE_TOPICS, "Topic"),
            (catalog.DELETE_TEMPLATES, "Template"),
            (catalog.DELETE_COMMUNITIES, "Community"),
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
