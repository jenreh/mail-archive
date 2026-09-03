"""The write halves against a real graph — upserts, and one edge per pair.

Idempotence is this phase's contract and it rests entirely on the catalogue's
statements being ``MERGE`` rather than ``CREATE``. ``session.add`` compiles to
``CREATE``, the derived labels carry no unique constraint, and a second rebuild
would therefore grow a second ``Group`` beside the first and hang every edge off
both. Nothing about that is visible in Python; it is visible in a node count,
which is why this file exists beside the pure ones.

``CO_ADDRESSED`` gets most of the attention because it is the only derived edge
between two ground-truth nodes and the only one that is undirected in meaning.
A Cypher edge always has a direction, so the write picks a canonical order and
the pattern carries no arrow — and both halves of that have to be true at once
or the same pair ends up stored twice under two names.
"""

import corpus
import pytest
from planted_graph import ground_truth
from runic.ogm import Session

from mailarc_analytics import (
    CoAddressedPair,
    CorrespondentFindings,
    GroupFacts,
    build_correspondents,
    build_topics,
    describe_templates,
    group_templates,
    write_correspondents,
    write_templates,
    write_topics,
)
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.rows import rows_of
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

CONFIG = corpus.calibrated_config()

DERIVED_EDGES = ("CO_ADDRESSED", "ADDRESSED_GROUP", "ABOUT", "INSTANCE_OF")


def _derived(session: Session) -> dict[str, int]:
    """Every derived label and edge type, counted the way the catalogue does.

    The three node counts go through ``rows_of``, which is how the package runs
    a catalogue statement: they are query-builder objects and the driver cannot
    be handed one. The four edge counts stay hand-written Cypher — they are the
    test's own question about what is in the store, not a claim about the
    catalogue.
    """
    counted = {
        name: int(
            rows_of(session, catalog.CATALOG[f"COUNT_{name.upper()}S"])[0]["total"]
        )
        for name in ("Group", "Topic", "Template")
    }
    counted |= {
        name: int(
            session.execute(f"MATCH ()-[r:{name}]->() RETURN count(r)").rows[0][0]
        )
        for name in DERIVED_EDGES
    }
    return counted


def _write_everything(config: GraphConfig) -> None:
    """All three analyses computed and written, exactly as a rebuild would."""
    facts = corpus.planted_facts()
    known = {one.id: one for one in facts}
    grouping = group_templates(facts, CONFIG)
    with client.session(config) as graph:
        write_correspondents(graph, build_correspondents(facts, CONFIG))
        write_topics(graph, build_topics(facts, CONFIG).clusters)
        write_templates(graph, describe_templates(grouping, known, {}, CONFIG))


class TestTheWritesAreUpserts:
    """Running each of them twice has to leave the same graph."""

    def test_the_first_write_produces_what_the_analyses_found(
        self, archived: GraphConfig
    ) -> None:
        """A count that never moves would also satisfy the test below."""
        _write_everything(archived)

        with client.session(archived) as graph:
            counted = _derived(graph)

        assert counted == {
            "Group": 2,
            "Topic": 1,
            "Template": 2,
            "CO_ADDRESSED": 3,
            "ADDRESSED_GROUP": 7,
            "ABOUT": 5,
            "INSTANCE_OF": 22,
        }

    def test_writing_the_same_findings_twice_changes_no_count(
        self, archived: GraphConfig
    ) -> None:
        _write_everything(archived)
        with client.session(archived) as graph:
            after_first = _derived(graph)

        _write_everything(archived)

        with client.session(archived) as graph:
            assert _derived(graph) == after_first

    def test_writing_them_twice_changes_no_property_either(
        self, archived: GraphConfig
    ) -> None:
        """A ``MERGE`` that matched and then overwrote with the same values is
        still an upsert; one that overwrote with different ones is a bug the
        counts would not show."""
        _write_everything(archived)
        with client.session(archived) as graph:
            before = graph.execute(
                "MATCH (n) WHERE n:Group OR n:Topic OR n:Template "
                "RETURN n.id, properties(n) ORDER BY n.id"
            ).rows

        _write_everything(archived)

        with client.session(archived) as graph:
            assert (
                graph.execute(
                    "MATCH (n) WHERE n:Group OR n:Topic OR n:Template "
                    "RETURN n.id, properties(n) ORDER BY n.id"
                ).rows
                == before
            )


class TestTheUndirectedEdge:
    """One unordered pair is one edge, however it was handed in."""

    def test_one_pair_becomes_exactly_one_edge(self, archived: GraphConfig) -> None:
        _write_everything(archived)

        with client.session(archived) as graph:
            both_ways = graph.execute(
                "MATCH (a:Address)-[r:CO_ADDRESSED]-(b:Address) RETURN count(r)"
            ).rows[0][0]
            one_way = graph.execute(
                "MATCH (a:Address)-[r:CO_ADDRESSED]->(b:Address) RETURN count(r)"
            ).rows[0][0]

        assert one_way == 3
        assert both_ways == 6  # the undirected pattern matches each edge twice

    def test_the_same_pair_handed_over_reversed_updates_the_same_edge(
        self, archived: GraphConfig
    ) -> None:
        """Which way round the edge was stored is an accident of who was
        written to first, so a rebuild that ordered a pair the other way must
        find it rather than grow a second one."""
        forwards = CorrespondentFindings(
            pairs=(CoAddressedPair(left=corpus.ANNA, right=corpus.THOMAS, count=1),)
        )
        backwards = CorrespondentFindings(
            pairs=(CoAddressedPair(left=corpus.THOMAS, right=corpus.ANNA, count=9),)
        )

        with client.session(archived) as graph:
            write_correspondents(graph, forwards)
        with client.session(archived) as graph:
            write_correspondents(graph, backwards)

        with client.session(archived) as graph:
            rows = graph.execute(
                "MATCH (:Address)-[r:CO_ADDRESSED]->(:Address) RETURN count(r), max(r.count)"
            ).rows

        assert rows[0] == [1, 9]

    def test_the_report_finds_the_pair_whichever_way_it_is_stored(
        self, archived: GraphConfig
    ) -> None:
        """The worked example of reading it without an arrow."""
        reversed_only = CorrespondentFindings(
            pairs=(CoAddressedPair(left=corpus.THOMAS, right=corpus.ANNA, count=4),)
        )

        with client.session(archived) as graph:
            write_correspondents(graph, reversed_only)
            rows = rows_of(graph, catalog.TOP_CO_ADDRESSED, {"limit": 10})

        assert [[row["left_id"], row["right_id"], row["together"]] for row in rows] == [
            [corpus.ANNA, corpus.THOMAS, 4]
        ]


class TestTheEdgesMatchTheirEndpoints:
    """``MATCH`` on both ends, never ``MERGE`` — the ordering bug stays visible."""

    def test_a_group_edge_for_a_group_that_is_not_there_writes_nothing(
        self, archived: GraphConfig
    ) -> None:
        """Merging the endpoint would paper over a caller's ordering mistake
        with an empty node, and every later read would see a group with no
        size and no dates."""
        orphan = CorrespondentFindings(
            groups=(
                GroupFacts(
                    id="never-written",
                    size=3,
                    message_count=2,
                    members=(corpus.canonical("p1"),),
                ),
            )
        )

        with client.session(archived) as graph:
            rows_of(
                graph,
                catalog.MERGE_ADDRESSED_GROUP,
                {
                    "rows": [
                        {
                            "message_id": corpus.canonical("p1"),
                            "group_id": orphan.groups[0].id,
                        }
                    ]
                },
            )
            counted = _derived(graph)

        assert counted["Group"] == 0
        assert counted["ADDRESSED_GROUP"] == 0

    def test_a_group_edge_for_a_message_that_is_not_there_writes_nothing(
        self, archived: GraphConfig
    ) -> None:
        """The derived layer may never invent a ``Message``."""
        with client.session(archived) as graph:
            write_correspondents(
                graph,
                CorrespondentFindings(
                    groups=(
                        GroupFacts(
                            id="circle", size=3, message_count=2, members=("gone",)
                        ),
                    )
                ),
            )
            counted = _derived(graph)
            messages = ground_truth(graph)["Message"]

        assert counted["Group"] == 1
        assert counted["ADDRESSED_GROUP"] == 0
        assert messages == 33


class TestTheGroundTruthIsUntouched:
    """A write may read a ``Message``; it may never change one."""

    def test_writing_everything_leaves_every_ground_truth_count_alone(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            before = ground_truth(graph)

        _write_everything(archived)

        with client.session(archived) as graph:
            assert ground_truth(graph) == before

    def test_the_archive_is_what_the_writer_made_of_the_corpus(
        self, archived: GraphConfig
    ) -> None:
        """Spelled out, so "unchanged" is measured against a known shape rather
        than against whatever the previous statement happened to leave.

        **One ``Thread`` per message, less the pair that share one.** The
        corpus is archived with a provider thread id on exactly two messages,
        and every other one now opens a conversation of its own keyed on its
        ``Message-ID`` — the writer's third key, and what makes a
        References-threaded mailbox groupable at all. So 33 ``IN_THREAD``
        edges and 32 threads, not 2 and 1. It is the storage cost of that fix,
        counted here rather than estimated.
        """
        with client.session(archived) as graph:
            counted = ground_truth(graph)

        assert counted == {
            "Message": 33,
            "Address": 8,
            "Thread": 32,
            "Label": 1,
            "Attachment": 1,
            "Account": 1,
            "SENT_FROM": 33,
            "SENT_TO": 36,
            "COPIED_TO": 2,
            "BLIND_COPIED_TO": 2,
            "IN_THREAD": 33,
            "REPLIES_TO": 1,
            "LABELED": 1,
            "HAS_ATTACHMENT": 2,
            "ARCHIVED_FROM": 33,
        }
