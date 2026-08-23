"""Every catalogue statement, compiled and run by the backend it was written for.

A Cypher string is only checked by a server. Everything the pure catalogue test
can say — that nothing is interpolated, that no ground-truth label is merged,
that the spec's ``a.address`` became ``a.id`` — it says by reading text, and
none of it would catch a missing comma, a property FalkorDB spells differently
or an aggregation whose grouping key is a list. So each of the thirty-three
entries is run here with its parameters bound and its result read.

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


def _prepare(session: Session) -> None:
    """The three derived nodes the edge upserts have to find, and the index the
    KNN needs.

    The upsert statements ``MATCH`` both endpoints instead of merging them, so
    running one against a graph without them writes nothing and would let a
    broken statement pass by writing nothing for the right reason.
    """
    session.execute(VECTOR_INDEX, {})
    for name in ("MERGE_GROUPS", "MERGE_TOPICS", "MERGE_TEMPLATES"):
        session.execute(CATALOG[name], _bind(name))


DDL = frozenset({"CREATE_VECTOR_INDEX", "DROP_VECTOR_INDEX", "CLEAR_EMBEDDINGS"})
"""The three that change the graph's shape rather than read or upsert it.

Excluded from the sweep below because running them in it would be dishonest in
both directions: ``DROP_VECTOR_INDEX`` would take away the index the KNN
statements in the same sweep need, and ``CREATE_VECTOR_INDEX`` would fail on
the index ``_prepare`` has already built. They get their own test, which
exercises the sequence they are actually used in.
"""


@pytest.mark.parametrize("name", sorted(set(CATALOG) - DDL))
def test_every_statement_runs_with_its_parameters_bound(
    archived: GraphConfig, name: str
) -> None:
    """Compiles, executes and comes back — for all thirty-three of them.

    The parameters come from the statement's own text through
    :func:`~mailarc_analytics.queries.catalog.parameters_of`, so a statement
    that gains one is bound here automatically or fails loudly with a missing
    key rather than running with a null.
    """
    with client.session(archived) as graph:
        _prepare(graph)

        result = graph.execute(CATALOG[name], _bind(name))

        assert result.rows is not None


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
    """The reader zips these names onto the rows, so a renamed column would
    reach it as a ``KeyError`` a long way from the statement."""
    with client.session(archived) as graph:
        _prepare(graph)

        result = graph.execute(CATALOG[name], _bind(name))

        assert list(result.columns) == columns


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
        rows = graph.execute(catalog.CO_RECIPIENTS, {"limit": 10}).rows

    counted, distinct_counted, rels = referenced.rows[0]

    assert len(rels) == 2, "the duplicate pairing is not in the graph"
    assert [int(counted), int(distinct_counted)] == [2, 1]
    assert [[left, right, int(together)] for left, right, together in rows] == [
        ["a@example.test", "b@example.test", 1]
    ]


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

        readable = graph.execute(catalog.COUNT_MESSAGES).rows[0][0]
        skipped = graph.execute(catalog.COUNT_UNIDENTIFIED).rows[0][0]
        rows = graph.execute(catalog.CO_RECIPIENTS, {"limit": 10}).rows

    assert [int(readable), int(skipped)] == [1, 2], "the planted graph is the point"
    assert [[left, right, int(together)] for left, right, together in rows] == [
        ["a@example.test", "b@example.test", 1]
    ]


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
        listed = graph.execute(catalog.TOP_CO_ADDRESSED, {"limit": 10}).rows

    assert unfiltered[0][2] is None, "the control: NULL really does sort first"
    assert [[left, right, int(together)] for left, right, together, *_ in listed] == [
        ["a@example.test", "b@example.test", 5]
    ]


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
    """
    spellings = ("count(m)", "count(*)", "count(DISTINCT m)")
    answers = []
    with client.session(archived) as graph:
        for spelling in spellings:
            rows = graph.execute(
                catalog.CO_RECIPIENTS.replace("count(m)", spelling), {"limit": 500}
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
            graph.execute(CATALOG[name], _bind(name))
        after_first = _counted(graph)
        for name in writes:
            graph.execute(CATALOG[name], _bind(name))

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
            graph.execute(CATALOG[name], _bind(name))

        removed = {
            name: [
                int(graph.execute(CATALOG[name], _bind(name)).rows[0][0])
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
        "Group": int(session.execute(catalog.COUNT_GROUPS).rows[0][0]),
        "Topic": int(session.execute(catalog.COUNT_TOPICS).rows[0][0]),
        "Template": int(session.execute(catalog.COUNT_TEMPLATES).rows[0][0]),
        "CO_ADDRESSED": int(session.execute(catalog.COUNT_CO_ADDRESSED).rows[0][0]),
    }
