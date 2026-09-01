"""Every catalogue statement, compiled and run by the backend it was written for.

A statement is only checked by a server. Everything the pure catalogue test can
say — that nothing is interpolated, that no ground-truth label is merged, that
the spec's ``a.address`` became ``a.id`` — it says by reading the compiled
Cypher, and none of it would catch a property FalkorDB spells differently, an
aggregation whose grouping key is a list, or a traversal that chained where it
should have branched. So every entry is run here with its parameters bound and
its result read.

Bound and never built, which is also what this file checks by doing it: a
catalogue statement declares its parameters rather than spelling them into its
text, so it is run through :func:`~mailarc_analytics.queries.rows.rows_of` —
``session.all_rows`` for the query-builder entries and ``session.execute`` for
the one that is still raw Cypher. Handing a builder to the driver raises
``TypeError``, and handing it ``statement.build()`` reaches the store without
the declared parameters; neither is a way to run one.

The archive underneath is the planted corpus, so the reads have something to
find and the ``MERGE`` statements have real ``Message`` and ``Address`` nodes to
attach to. What the statements *answer* is asserted where the analyses are
tested; this file only asks whether they run and return the columns they name.
"""

from typing import Any

import corpus
import pytest
from runic.ogm import Session

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import CATALOG, parameters_of
from mailarc_analytics.queries.rows import rows_of
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

GROUP_ID = "circle-under-test"
TOPIC_ID = "topic:0000000000000000000000000000test"
TEMPLATE_ID = "template:0000000000000001:sent"

DIMENSION = 4
"""Floats per vector in this file — not the shipped 768.

Four keeps the bound parameter readable. That the *shipped* number matches the
migration is asserted in ``tests/semantic/test_semantic_config.py``, by reading
the migration's own source; here the question is only whether the statements
compile and run.
"""

SCALARS: dict[str, Any] = {
    "after": "",
    "limit": 10,
    "batch": 100,
    "min_size": 3,
    "min_messages": 2,
    "direction": "sent",
    "ids": [corpus.canonical("p1")],
    "model": "probe-model",
    "max_chars": 200,
    "k": 5,
    "max_distance": 0.18,
    "vector": [1.0, 0.0, 0.0, 0.0],
    "text": "rechnung",
}
"""What every non-row parameter is bound to. Types matter: an ``$ids`` bound to
a string rather than a list compiles and then matches nothing, and a ``$vector``
whose length disagrees with the index is refused by the KNN outright."""

ROWS: dict[str, list[dict[str, Any]]] = {
    "MERGE_GROUPS": [
        {
            "id": GROUP_ID,
            "size": 3,
            "message_count": 5,
            "first_seen": "2026-01-12T09:00:00+00:00",
            "last_seen": "2026-02-10T16:00:00+00:00",
        }
    ],
    "MERGE_ADDRESSED_GROUP": [
        {"message_id": corpus.canonical("p1"), "group_id": GROUP_ID}
    ],
    "MERGE_CO_ADDRESSED": [
        {
            "left": corpus.ANNA,
            "right": corpus.THOMAS,
            "count": 2,
            "first_seen": "2026-01-12T09:00:00+00:00",
            "last_seen": "2026-02-03T07:45:00+00:00",
        }
    ],
    "MERGE_TOPICS": [
        {
            "id": TOPIC_ID,
            "label": "angebot datenmigration",
            "method": "ref",
            "score": 1.0,
            "message_count": 5,
            "first_seen": "2026-01-12T09:00:00+00:00",
            "last_seen": "2026-02-10T16:00:00+00:00",
        }
    ],
    "MERGE_ABOUT": [
        {
            "message_id": corpus.canonical("p1"),
            "topic_id": TOPIC_ID,
            "score": 1.0,
            "method": "ref",
        }
    ],
    "MERGE_TEMPLATES": [
        {
            "id": TEMPLATE_ID,
            "sample_text": "Hallo zusammen,",
            "occurrences": 12,
            "automation_score": 0.641072,
            "direction": "sent",
            "first_seen": "2026-01-05T08:00:00+00:00",
            "last_seen": "2026-12-07T08:00:00+00:00",
        }
    ],
    "WRITE_EMBEDDINGS": [
        {"id": corpus.canonical("p1"), "vector": [1.0, 0.0, 0.0, 0.0]}
    ],
    "MERGE_INSTANCE_OF": [
        {
            "message_id": corpus.canonical("s01"),
            "template_id": TEMPLATE_ID,
            "distance": 0,
        }
    ],
}
"""One representative row per upsert, with exactly the keys its docstring names.

A missing key is not an error in Cypher — it binds to null and silently blanks
the property — so the rows are written out per statement rather than shared,
and running them here is what turns each docstring into a checked claim.
"""


def _bind(name: str) -> dict[str, Any]:
    """Every parameter *name* declares, with a value of the right type."""
    return {
        parameter: ROWS[name] if parameter == "rows" else SCALARS[parameter]
        for parameter in parameters_of(CATALOG[name])
    }


VECTOR_INDEX = (
    "CREATE VECTOR INDEX FOR (n:Message) ON (n.embedding) "
    f"OPTIONS {{dimension: {DIMENSION}, similarityFunction: 'cosine'}}"
)
"""The schema the vector-index migration creates, issued here by hand.

It cannot come from the catalogue: a test next door asserts that no statement
in that file contains ``CREATE``, because a file that can create an index is a
file that can create a node. And it cannot come from the migration either — a
component may not read the composed application configuration ``runic`` needs
to find a graph. So the KNN statement gets the index it requires from the
fixture that runs it, or ``db.idx.vector.queryNodes`` raises
``Attempted to access undefined attribute`` and the statement would look broken
when only the schema was missing.
"""


FULLTEXT_INDEX = (
    "CALL db.idx.fulltext.createNodeIndex('Message', 'subject', 'body_text')"
)
"""The index the baseline migration creates, issued here for the same reason
:data:`VECTOR_INDEX` is: without it ``db.idx.fulltext.queryNodes`` answers with
no rows at all rather than raising, which is exactly what a full-text statement
that had stopped working would look like."""


def _prepare(session: Session) -> None:
    """Everything a statement needs to find something: the derived nodes the
    edge upserts have to match, the two indexes the search procedures read, and
    one stored vector for the KNN.

    The upsert statements ``MATCH`` both endpoints instead of merging them, so
    running one against a graph without them writes nothing and would let a
    broken statement pass by writing nothing for the right reason. The same
    argument covers the rest: a statement that comes back empty proves only
    that it parsed, so the graph is planted until every read has a row to
    return and the column names below are read off a real one.
    """
    session.execute(VECTOR_INDEX, {})
    session.execute(FULLTEXT_INDEX, {})
    for name in ("MERGE_GROUPS", "MERGE_TOPICS", "MERGE_TEMPLATES"):
        rows_of(session, CATALOG[name], _bind(name))
    for name in ("MERGE_ADDRESSED_GROUP", "MERGE_ABOUT", "MERGE_INSTANCE_OF"):
        rows_of(session, CATALOG[name], _bind(name))
    rows_of(session, CATALOG["MERGE_CO_ADDRESSED"], _bind("MERGE_CO_ADDRESSED"))
    rows_of(session, CATALOG["WRITE_EMBEDDINGS"], _bind("WRITE_EMBEDDINGS"))


NOT_IN_THE_SWEEP = frozenset({"CLEAR_EMBEDDINGS"})
"""The one entry that changes the graph's shape rather than reading or
upserting it.

Excluded because running it in the sweep would take every vector away from the
KNN statements beside it, and then a statement that had stopped finding
anything would still pass. It is exercised where it is used, in
``tests/semantic/test_semantic_indexing_local.py``, as the middle step of a
re-index.

``CREATE_VECTOR_INDEX`` and ``DROP_VECTOR_INDEX`` used to be named here too.
They are functions now rather than statements — runic 0.5 emits vector-index
DDL through ``IndexOperations`` — so they are not in ``CATALOG`` at all and
there is nothing left to exclude. The same file tests them, in the drop /
clear / create order ``rebuild_index`` runs them in.
"""


@pytest.mark.parametrize("name", sorted(set(CATALOG) - NOT_IN_THE_SWEEP))
def test_every_statement_runs_with_its_parameters_bound(
    archived: GraphConfig, name: str
) -> None:
    """Compiles, executes and comes back — for every entry in the catalogue.

    The parameters come from the statement itself through
    :func:`~mailarc_analytics.queries.catalog.parameters_of`, which asks a
    builder what it declared and reads the one raw statement's text, so a
    statement that gains one is bound here automatically. A binding that left
    one out would raise ``ValueError: statement is missing values for declared
    parameter(s)`` rather than run with a null — which is the security property
    the catalogue exists for, exercised here on every entry at once.
    """
    with client.session(archived) as graph:
        _prepare(graph)

        rows = rows_of(graph, CATALOG[name], _bind(name))

        assert isinstance(rows, list)


@pytest.mark.parametrize(
    ("name", "columns"),
    [
        ("ACCOUNT_ADDRESSES", ["address"]),
        (
            "MESSAGE_PROPERTIES",
            ["id", "sent_at", "subject_norm", "participant_key", "simhash", "refs"],
        ),
        (
            "MESSAGE_RELATIONS",
            ["id", "senders", "addressed", "blind_copied", "threads", "attachments"],
        ),
        ("MESSAGE_BODIES", ["id", "body_clean"]),
        ("ARCHIVED_PER_DAY", ["day", "messages", "bytes"]),
        ("COUNT_UNIDENTIFIED", ["total"]),
        ("CO_RECIPIENTS", ["left_id", "right_id", "together"]),
        (
            "TOP_CO_ADDRESSED",
            ["left_id", "right_id", "together", "first_seen", "last_seen"],
        ),
        (
            "RECURRING_GROUPS",
            ["id", "size", "message_count", "first_seen", "last_seen"],
        ),
        (
            "TOP_TEMPLATES",
            [
                "id",
                "occurrences",
                "automation_score",
                "sample_text",
                "first_seen",
                "last_seen",
            ],
        ),
        ("TOPIC_BREAKDOWN", ["id", "label", "method", "messages"]),
        ("COUNT_NEEDING_EMBEDDING", ["total"]),
        ("MESSAGES_NEEDING_EMBEDDING", ["id", "subject", "body"]),
        ("VECTOR_COVERAGE", ["total", "embedded", "unembeddable"]),
        (
            "SEMANTIC_NEIGHBOURS",
            ["id", "subject", "sent_at", "sender", "distance"],
        ),
        (
            "FULLTEXT_MESSAGES",
            ["id", "subject", "sent_at", "sender", "relevance"],
        ),
        ("VECTOR_INDEX_OPTIONS", ["label", "properties", "types", "options"]),
    ],
)
def test_a_reading_statement_returns_the_columns_it_names(
    archived: GraphConfig, name: str, columns: list[str]
) -> None:
    """Every consumer reads ``row["together"]``, so a renamed column would
    reach it as a ``KeyError`` a long way from the statement.

    Read off a **real row** rather than off the result header, which is what
    ``all_rows`` leaves a caller: it answers with column-keyed dicts and a
    statement that matched nothing has no keys to show. That makes the
    assertion stronger than it was — an empty answer no longer passes it — and
    it is why ``_prepare`` plants enough for every one of these to find
    something. It is also the assertion that catches a computed column whose
    ``.as_()`` went missing: without one the row is keyed by the raw Cypher,
    ``collect(DISTINCT s.id)`` instead of ``senders``.
    """
    with client.session(archived) as graph:
        _prepare(graph)

        rows = rows_of(graph, CATALOG[name], _bind(name))

        assert rows, "a statement that returns nothing cannot show its columns"
        assert list(rows[0]) == columns


def test_a_recipient_in_both_to_and_cc_is_counted_once(config: GraphConfig) -> None:
    """A2's definition counts messages, not the rows its pattern matches.

    An address in both ``To`` and ``Cc`` has two edges from the same message,
    so ``(a)<-[…]-(m)-[…]->(b)`` looks like it must match that message twice
    and inflate the truth side of the cross-check. It does not, because the
    statement never *references* either relationship and FalkorDB prunes the
    duplicate binding before the aggregation sees it.

    The control is the same pattern with a relationship referenced, and it is
    here because that is the only thing that makes the assertion a measurement:
    it shows the duplicate really is in the graph and that ``count(m)`` really
    does count it once the planner can no longer prune it. So this pins a
    property of the backend rather than of the text — and it fails the day
    somebody adds a column naming which header carried the pair without
    switching to ``count(DISTINCT m)``.
    """
    with client.session(config) as graph:
        graph.execute(
            "CREATE (m:Message {id: 'm1'}), "
            "(a:Address {id: 'a@example.test'}), "
            "(b:Address {id: 'b@example.test'}), "
            "(m)-[:SENT_TO]->(a), (m)-[:SENT_TO]->(b), (m)-[:COPIED_TO]->(b)"
        )

        referenced = graph.execute(
            "MATCH (a:Address)<-[r1:SENT_TO|COPIED_TO]-(m:Message)"
            "-[r2:SENT_TO|COPIED_TO]->(b:Address) "
            "WHERE a.id < b.id "
            "RETURN count(m) AS counted, count(DISTINCT m) AS distinct_counted, "
            "collect(id(r2)) AS rels"
        )
        rows = rows_of(graph, catalog.CO_RECIPIENTS, {"limit": 10})

    counted, distinct_counted, rels = referenced.rows[0]

    assert len(rels) == 2, "the duplicate pairing is not in the graph"
    assert [int(counted), int(distinct_counted)] == [2, 1]
    assert _pairs(rows) == [["a@example.test", "b@example.test", 1]]


def test_a_message_the_rebuild_skips_is_not_counted_as_truth(
    config: GraphConfig,
) -> None:
    """Both sides of the cross-check must count the same population.

    ``MESSAGE_PROPERTIES`` — what a rebuild actually reads — takes only nodes
    with a non-empty canonical id, and ``COUNT_UNIDENTIFIED`` exists to say how
    many it stepped over. A1's ground-truth half had no such filter, so it
    counted nodes the edge can never represent: the truth side came out higher
    than the edge by construction, the verdict went permanently yellow, and the
    three innocent causes it names — no rebuild yet, a message ceiling, a
    wide-recipient mail — explained none of it. No rebuild could clear it.
    """
    with client.session(config) as graph:
        graph.execute(
            "CREATE (good:Message {id: 'good'}), (none:Message), "
            "(blank:Message {id: ''}), "
            "(a:Address {id: 'a@example.test'}), "
            "(b:Address {id: 'b@example.test'}), "
            "(good)-[:SENT_TO]->(a), (good)-[:SENT_TO]->(b), "
            "(none)-[:SENT_TO]->(a), (none)-[:SENT_TO]->(b), "
            "(blank)-[:SENT_TO]->(a), (blank)-[:SENT_TO]->(b)"
        )

        readable = rows_of(graph, catalog.COUNT_MESSAGES)[0]["total"]
        skipped = rows_of(graph, catalog.COUNT_UNIDENTIFIED)[0]["total"]
        rows = rows_of(graph, catalog.CO_RECIPIENTS, {"limit": 10})

    assert [int(readable), int(skipped)] == [1, 2], "the planted graph is the point"
    assert _pairs(rows) == [["a@example.test", "b@example.test", 1]]


def test_an_edge_with_no_count_cannot_take_the_top_slot(
    config: GraphConfig,
) -> None:
    """A NULL sorts ahead of every real count on this backend's ``DESC``.

    Measured rather than assumed, because it is the whole reason for the
    filter: without it the countless edge is listed *first*, decodes to 0, and
    ``_floor`` then reads that 0 as "this listing was never cut" — so a
    truncated listing claims to be exhaustive and the cross-check reports pairs
    it had no business ruling on.
    """
    with client.session(config) as graph:
        graph.execute(
            "CREATE (a:Address {id: 'a@example.test'}), "
            "(b:Address {id: 'b@example.test'}), "
            "(c:Address {id: 'c@example.test'}), "
            "(a)-[:CO_ADDRESSED {count: 5}]->(b), "
            "(a)-[:CO_ADDRESSED]->(c)"
        )

        unfiltered = graph.execute(
            "MATCH (a:Address)-[r:CO_ADDRESSED]-(b:Address) WHERE a.id < b.id "
            "RETURN a.id, b.id, r.count AS together ORDER BY together DESC LIMIT 1"
        ).rows
        listed = rows_of(graph, catalog.TOP_CO_ADDRESSED, {"limit": 10})

    assert unfiltered[0][2] is None, "the control: NULL really does sort first"
    assert _pairs(listed) == [["a@example.test", "b@example.test", 5]]


def test_the_three_spellings_of_a1s_count_agree_on_this_statement(
    archived: GraphConfig,
) -> None:
    """The claim ``CoAddressedAgreement``'s docstring makes, measured.

    It reasons that ``CO_RECIPIENTS`` never *references* either relationship,
    so the backend prunes the duplicate ``(a, m, b)`` binding and ``count(m)``,
    ``count(*)`` and ``count(DISTINCT m)`` all answer the same thing. That
    paragraph is load-bearing — it tells a future maintainer when they must
    switch to ``count(DISTINCT m)`` — and it was asserted rather than proved.

    Proving it is not academic. Against the statement as it stood *before* the
    canonical-id filter, ``count(*)`` answered 1 where ``count(m)`` answered 2
    on this very corpus: the equivalence held for the shape of the statement,
    not for the two spellings, and mentioning ``m.id`` in the ``WHERE`` is part
    of what makes it hold. So this pins the whole statement rather than the
    reasoning about it, and goes red if either the text or the backend moves.

    The one spelling is varied in the statement's **compiled** Cypher, because
    a statement is a builder now and there is no text on it to edit. Compiling
    it is also what makes the variants honest: they differ from the catalogue
    entry in three characters and in nothing else. They are run raw, which
    needs the auto-bound literals ``build()`` hands back — the empty-string
    comparison is ``$p0`` — merged into the binding beside ``$limit``, since
    the store is being given text rather than a statement that knows its own
    parameters. That merging is exactly the step ``rows_of`` exists to make
    unnecessary everywhere else.
    """
    spellings = ("count(m)", "count(*)", "count(DISTINCT m)")
    cypher, bound = catalog.CO_RECIPIENTS.build()
    answers = []
    with client.session(archived) as graph:
        for spelling in spellings:
            rows = graph.execute(
                cypher.replace("count(m)", spelling), {**bound, "limit": 500}
            ).rows
            answers.append(
                sorted((left, right, int(together)) for left, right, together in rows)
            )

    assert answers[0], "the corpus has to hold co-addressed pairs for this to mean any"
    assert answers[0] == answers[1] == answers[2], dict(
        zip(spellings, answers, strict=True)
    )


def test_the_upserts_really_are_upserts_on_this_backend(
    archived: GraphConfig,
) -> None:
    """Run every write twice and count. ``MERGE`` is the whole idempotence
    contract, and whether a given dialect honours it on an unconstrained label
    is not something a string can promise."""
    writes = sorted(ROWS)

    with client.session(archived) as graph:
        for name in writes:
            rows_of(graph, CATALOG[name], _bind(name))
        after_first = _counted(graph)
        for name in writes:
            rows_of(graph, CATALOG[name], _bind(name))

        assert _counted(graph) == after_first


def test_every_delete_drains_and_then_stops(archived: GraphConfig) -> None:
    """The loop condition is the statement's own ``RETURN count(…)``.

    A delete that reported a non-zero count forever would spin; one that
    reported zero while rows remained would leave the derived layer behind for
    the next rebuild to merge into.
    """
    deletes = (
        "DELETE_GROUPS",
        "DELETE_TOPICS",
        "DELETE_TEMPLATES",
        "DELETE_CO_ADDRESSED",
    )

    with client.session(archived) as graph:
        for name in sorted(ROWS):
            rows_of(graph, CATALOG[name], _bind(name))

        removed = {
            name: [
                int(rows_of(graph, CATALOG[name], _bind(name))[0]["removed"])
                for _ in range(2)
            ]
            for name in deletes
        }

        assert removed == {name: [1, 0] for name in deletes}
        assert _counted(graph) == {
            "Group": 0,
            "Topic": 0,
            "Template": 0,
            "CO_ADDRESSED": 0,
        }


def _counted(session: Session) -> dict[str, int]:
    """The four derived counts, straight out of the catalogue."""
    return {
        "Group": int(rows_of(session, catalog.COUNT_GROUPS)[0]["total"]),
        "Topic": int(rows_of(session, catalog.COUNT_TOPICS)[0]["total"]),
        "Template": int(rows_of(session, catalog.COUNT_TEMPLATES)[0]["total"]),
        "CO_ADDRESSED": int(rows_of(session, catalog.COUNT_CO_ADDRESSED)[0]["total"]),
    }


def _pairs(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """The three columns both A1 readings share, by name.

    Named rather than sliced: ``all_rows`` keys a row by its column, so a test
    that unpacked positionally would be asserting the order of a projection
    instead of the columns a consumer actually reads.
    """
    return [[row["left_id"], row["right_id"], int(row["together"])] for row in rows]
