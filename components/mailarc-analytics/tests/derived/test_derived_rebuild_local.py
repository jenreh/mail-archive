"""``rebuild-derived`` end to end — the phase's definition of done, measured.

Three claims, and only a real graph can settle any of them. That the rebuild
finds exactly what the corpus planted. That running it a second time changes
*nothing* — not a node id, not an edge, not a property value, which is a
stricter statement than "the counts match" and the one §5.2 actually makes.
And that it never touches the ground truth, so an analysis bug costs one run
rather than a restore.

The graph is deliberately dirtied first. A ``CO_ADDRESSED`` edge naming an
address that must never be co-addressed, and three derived nodes belonging to
no analysis, are planted before the first rebuild — because a rebuild that only
ever wrote into an empty derived layer would prove nothing about the delete
half, and the delete half is the one that could take the archive with it.
"""

import corpus
import pytest
from planted_graph import archive, archive_refingerprinted, ground_truth
from runic.ogm import Session

from mailarc_analytics import (
    DerivedCounts,
    RebuildProgress,
    RebuildStage,
    rebuild_derived,
)
from mailarc_analytics.derived import rebuild
from mailarc_analytics.derived.model import EMBEDDING_METHOD, SimilarityEdge
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.rows import rows_of
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

CONFIG = corpus.calibrated_config()

STALE_COUNT = 999
"""A count no analysis would produce, so a survivor is unmistakable."""

SIGN_BIT = 1 << 63


def _snapshot(session: Session) -> dict[str, object]:
    """Everything derived, down to the property values, in a stable order.

    Ids and counts would miss the case that matters most: a rebuild that wrote
    the same nodes with a different score, or hung the same edge off them with
    a different method. Two runs have to agree about all of it.
    """
    found: dict[str, object] = {
        label: [
            row[0]
            for row in session.execute(
                f"MATCH (n:{label}) RETURN properties(n) ORDER BY n.id"
            ).rows
        ]
        for label in ("Group", "Topic", "Template")
    }
    found["CO_ADDRESSED"] = [
        row[0]
        for row in session.execute(
            "MATCH (a:Address)-[r:CO_ADDRESSED]-(b:Address) WHERE a.id < b.id "
            "RETURN [a.id, b.id, properties(r)] ORDER BY a.id, b.id"
        ).rows
    ]
    for edge, target in (("ABOUT", "Topic"), ("INSTANCE_OF", "Template")):
        found[edge] = [
            row[0]
            for row in session.execute(
                f"MATCH (m:Message)-[r:{edge}]->(t:{target}) "
                f"RETURN [m.id, t.id, properties(r)] ORDER BY m.id, t.id"
            ).rows
        ]
    found["ADDRESSED_GROUP"] = [
        row[0]
        for row in session.execute(
            "MATCH (m:Message)-[:ADDRESSED_GROUP]->(g:Group) "
            "RETURN [m.id, g.id] ORDER BY m.id, g.id"
        ).rows
    ]
    return found


def _dirty(session: Session) -> None:
    """Leave behind exactly what a previous, different rebuild would have.

    A pair that must not exist — the Bcc'd address, co-addressed with the one
    visible recipient — and one node of each derived label under a key no
    analysis will mint again.
    """
    session.execute(
        "MATCH (a:Address {id: $left}), (b:Address {id: $right}) "
        "MERGE (a)-[r:CO_ADDRESSED]-(b) SET r.count = $count",
        {"left": corpus.ANNA, "right": corpus.REVISION, "count": STALE_COUNT},
    )
    for label in ("Group", "Topic", "Template"):
        session.execute(
            f"MERGE (n:{label} {{id: 'stale'}}) SET n.message_count = $count",
            {"count": STALE_COUNT},
        )


def _rebuild(config: GraphConfig) -> DerivedCounts:
    with client.session(config) as graph:
        return rebuild_derived(graph, CONFIG)


def _pair(row: dict[str, object]) -> tuple[object, object, object]:
    """The three columns the two A1 readings have in common.

    Named rather than sliced. Both statements are query-builder objects now and
    ``rows_of`` keys every row by its column, so the comparison says which
    three columns it is comparing instead of trusting them to stay in the first
    three positions of both projections.
    """
    return row["left_id"], row["right_id"], row["together"]


class TestWhatOneRebuildFinds:
    """Exactly what was planted — five findings out of thirty-three messages."""

    def test_the_counts_are_the_ones_the_three_analyses_report(
        self, archived: GraphConfig
    ) -> None:
        counts = _rebuild(archived)

        assert counts == DerivedCounts(
            messages=33,
            beyond_ceiling=0,
            unidentified=0,
            groups=2,
            co_addressed=3,
            wide_messages=0,
            topics=1,
            dropped_buckets=0,
            dropped_weak_pairs=0,
            templates=2,
            unhashable_messages=0,
            dropped_template_buckets=0,
            deleted_nodes=0,
            deleted_edges=0,
        )

    def test_the_topic_is_the_project_and_carries_its_reason(
        self, archived: GraphConfig
    ) -> None:
        """§12's topic breakdown, run against what the rebuild wrote."""
        _rebuild(archived)

        with client.session(archived) as graph:
            rows = rows_of(graph, catalog.TOPIC_BREAKDOWN, {"limit": 10})

        assert rows == [
            {
                "id": "topic:8ddcd22af04394667b0b8bfef1d1a97e",
                "label": "angebot datenmigration",
                "method": "ref",
                "messages": 5,
            }
        ]

    def test_the_templates_are_reported_one_direction_at_a_time(
        self, archived: GraphConfig
    ) -> None:
        """Only what the user writes themselves is worth automating, and the
        scores are only comparable within one direction."""
        _rebuild(archived)

        with client.session(archived) as graph:
            sent = rows_of(
                graph, catalog.TOP_TEMPLATES, {"direction": "sent", "limit": 10}
            )
            received = rows_of(
                graph, catalog.TOP_TEMPLATES, {"direction": "received", "limit": 10}
            )

        assert [
            (row["id"], row["occurrences"], row["automation_score"]) for row in sent
        ] == [("template:1e164feec6258562:sent", 12, 0.641072)]
        assert [
            (row["id"], row["occurrences"], row["automation_score"]) for row in received
        ] == [("template:132b71d16ae83c39:received", 10, 0.279724)]

    def test_the_recurring_groups_answer_uses_the_configured_thresholds(
        self, archived: GraphConfig
    ) -> None:
        """The spec's literals ``> 2`` and ``> 5`` are parameters here, or
        half of ``AnalyticsConfig`` would be decorative."""
        _rebuild(archived)

        with client.session(archived) as graph:
            rows = rows_of(
                graph,
                catalog.RECURRING_GROUPS,
                {
                    "min_size": CONFIG.min_group_size,
                    "min_messages": CONFIG.min_group_messages,
                    "limit": 10,
                },
            )

        assert [(row["id"], row["size"], row["message_count"]) for row in rows] == [
            (corpus.circle_of("p1"), 3, 5),
            (corpus.circle_of("b1"), 3, 2),
        ]

    def test_the_materialised_pairs_agree_with_the_query_that_defines_them(
        self, archived: GraphConfig
    ) -> None:
        """§6.1's self-join is the definition; the edge is the materialisation.

        If the two ever disagree, the edge is the one that is wrong — so the
        test compares them rather than asserting the edge alone.
        """
        _rebuild(archived)

        with client.session(archived) as graph:
            defined = rows_of(graph, catalog.CO_RECIPIENTS, {"limit": 50})
            stored = rows_of(graph, catalog.TOP_CO_ADDRESSED, {"limit": 50})

        assert sorted(_pair(row) for row in defined) == sorted(
            _pair(row) for row in stored
        )
        assert len(stored) == 3


class TestSignalSixReachesTheRebuild:
    """The forwarding that had no caller for a whole phase.

    ``build_topics`` took ``extra_edges`` and ``rebuild_derived`` had no
    parameter to pass one through, so signal 6 was a tested capability nothing
    in the application could reach. These two tests are the pin: one that the
    suggestion arrives and is written onto the edge as a suggestion, one that
    handing in nothing leaves the phase-5 rebuild untouched.
    """

    def test_a_handed_in_pair_joins_what_the_exact_signals_left_open(
        self, archived: GraphConfig
    ) -> None:
        """Two messages nothing else connects, joined by an embedding edge and
        labelled as the guess it is — so a reader can tell it from a topic a
        shared ticket token drew."""
        with client.session(archived) as graph:
            loose = [
                row["id"]
                for row in rows_of(
                    graph, catalog.MESSAGE_PROPERTIES, {"after": "", "limit": 10_000}
                )
            ]
        joined = _lonely_pair(archived, loose)

        with client.session(archived) as graph:
            counts = rebuild_derived(
                graph,
                CONFIG,
                extra_edges=[
                    SimilarityEdge(
                        left=joined[0],
                        right=joined[1],
                        method=EMBEDDING_METHOD,
                        weight=0.93,
                    )
                ],
            )
            methods = [
                row["method"]
                for row in rows_of(graph, catalog.TOPIC_BREAKDOWN, {"limit": 50})
            ]

        assert counts.topics == 2, "the suggestion made a topic of its own"
        assert EMBEDDING_METHOD in methods

    def test_handing_in_nothing_is_the_phase_five_rebuild(
        self, archived: GraphConfig
    ) -> None:
        """§10's "all phase-5 analyses run unchanged", asserted rather than
        argued: the default really is the old behaviour."""
        with client.session(archived) as graph:
            explicit = rebuild_derived(graph, CONFIG, extra_edges=())
        with client.session(archived) as graph:
            defaulted = rebuild_derived(graph, CONFIG)

        assert explicit.topics == defaulted.topics
        assert defaulted.topics == 1


def _lonely_pair(config: GraphConfig, ids: list[str]) -> tuple[str, str]:
    """Two message ids that no exact signal connects.

    Found by rebuilding once and taking two messages that ended in no topic at
    all — a pair the five signals really did leave open, rather than two ids
    picked out of the corpus and hoped about.
    """
    with client.session(config) as graph:
        rebuild_derived(graph, CONFIG)
        clustered = {
            row["id"]
            for row in rows_of(
                graph,
                "MATCH (t:Topic)<-[:ABOUT]-(m:Message) RETURN m.id AS id",
                {},
            )
        }
    free = sorted(set(ids) - clustered)
    assert len(free) >= 2, "the corpus has to leave two messages unclustered"
    return free[0], free[1]


class TestTheDeleteHalf:
    """What a rebuild removes, and what it must leave exactly where it is."""

    def test_a_stale_pair_from_an_earlier_run_is_gone(
        self, archived: GraphConfig
    ) -> None:
        """The Bcc'd address was co-addressed by a previous, wrong rebuild.

        A ``MERGE``-only recompute would leave it there forever, which is why
        ``rebuild-derived`` deletes before it writes rather than upserting over
        whatever it finds.
        """
        with client.session(archived) as graph:
            _dirty(graph)

        _rebuild(archived)

        with client.session(archived) as graph:
            rows = rows_of(graph, catalog.TOP_CO_ADDRESSED, {"limit": 50})

        assert corpus.REVISION not in {row["left_id"] for row in rows} | {
            row["right_id"] for row in rows
        }
        assert STALE_COUNT not in {row["together"] for row in rows}

    def test_stale_derived_nodes_are_gone_and_counted(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            _dirty(graph)

        counts = _rebuild(archived)

        with client.session(archived) as graph:
            leftovers = graph.execute(
                "MATCH (n) WHERE n.id = 'stale' RETURN count(n)"
            ).rows[0][0]

        assert (counts.deleted_nodes, counts.deleted_edges) == (3, 1)
        assert leftovers == 0

    def test_deleting_the_pair_leaves_both_addresses_standing(
        self, archived: GraphConfig
    ) -> None:
        """``CO_ADDRESSED`` is the one derived thing between two ground-truth
        nodes. ``DETACH DELETE`` there would take the address book with it."""
        with client.session(archived) as graph:
            _dirty(graph)
            before = ground_truth(graph)

        _rebuild(archived)

        with client.session(archived) as graph:
            assert ground_truth(graph) == before

    def test_a_rebuild_of_an_archive_with_no_derived_layer_deletes_nothing(
        self, archived: GraphConfig
    ) -> None:
        """The delete loop's termination condition, seen from the other end."""
        counts = _rebuild(archived)

        assert (counts.deleted_nodes, counts.deleted_edges) == (0, 0)


class TestTheSignTrapEndToEnd:
    """The phase's own most-likely bug, caught where it would actually happen.

    Every other test of the trap stops short of a full run: the pure ones build
    their facts from the parser rather than from a graph, and the corpus's two
    template families both hash *positive*, so the whole of A3's real-graph path
    runs over the easy half of the value range. The five planted messages that
    do store negative are all outside both families.

    So this is the missing link — the negative family written by the real
    writer, read by the real reader and clustered by the real A3 in one run.
    Take :func:`~mailarc_core.archive.model.to_unsigned_64` out of the reader
    and this is what fails, rather than an archive quietly reporting that
    nothing in it repeats.
    """

    def test_a_family_stored_negative_still_becomes_one_template(
        self, config: GraphConfig
    ) -> None:
        archive(config, corpus.top_bit_messages())

        counts = _rebuild(config)

        with client.session(config) as graph:
            stored = graph.execute(
                "MATCH (m:Message) WHERE m.simhash < 0 RETURN count(m)"
            ).rows[0][0]
            rows = rows_of(
                graph, catalog.TOP_TEMPLATES, {"direction": "sent", "limit": 10}
            )

        assert stored == 3
        assert counts.templates == 1
        assert [(row["id"], row["occurrences"]) for row in rows] == [
            ("template:a0b86145044638a0:sent", 3)
        ]

    def test_its_key_carries_no_minus_sign_after_the_round_trip(
        self, config: GraphConfig
    ) -> None:
        """``f"{-5812…:016x}"`` renders one, and two runs disagreeing about a
        minus sign are not a key — which is how an unconverted fingerprint
        would break idempotence rather than clustering."""
        archive(config, corpus.top_bit_messages())
        _rebuild(config)

        with client.session(config) as graph:
            keys = [
                row[0] for row in graph.execute("MATCH (t:Template) RETURN t.id").rows
            ]

        assert keys == ["template:a0b86145044638a0:sent"]

    def test_a_family_that_straddles_the_sign_is_still_one_template(
        self, config: GraphConfig
    ) -> None:
        """The case that actually breaks, and the reason the two above are not
        enough on their own.

        An all-negative family survives an unconverted read by accident:
        ``(negative ^ negative)`` is positive, so the Hamming distance comes
        out right, and both ``band_keys`` and ``template_id`` convert again for
        themselves. It is the *mixed* pair that answers twenty-eight where the
        truth is four — and then A3 reports an archive with nothing repetitive
        in it, which is a far quieter failure than a wrong cluster.

        Two of the three fingerprints have bit 63 flipped, so the graph stores
        one negative and two positive and the three are still four or five bits
        apart as unsigned values.
        """
        planted = corpus.top_bit_messages()
        base = [corpus.facts_of(one).simhash for one in planted]
        straddling = [base[0], *(one ^ SIGN_BIT for one in base[1:])]

        archive_refingerprinted(config, planted, straddling)
        counts = _rebuild(config)

        with client.session(config) as graph:
            signs = {
                row[0] < 0
                for row in graph.execute("MATCH (m:Message) RETURN m.simhash").rows
            }
            rows = rows_of(
                graph, catalog.TOP_TEMPLATES, {"direction": "sent", "limit": 10}
            )

        assert signs == {True, False}
        assert counts.templates == 1
        assert [(row["id"], row["occurrences"]) for row in rows] == [
            ("template:a0b86145044638a0:sent", 3)
        ]


class TestWhatACeilingLeavesOut:
    """``max_messages`` was the one omission in this package nothing counted.

    ``unidentified``, ``wide_messages``, ``unhashable_messages`` and both
    dropped-bucket numbers all reach the job row, and ``DerivedCounts`` argues
    why: a rebuild that dropped something without saying so looks exactly like
    a rebuild that found nothing to say. A capped one used to look exactly like
    a rebuild of a small archive.
    """

    def test_a_capped_rebuild_says_how_much_it_never_looked_at(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            counts = rebuild_derived(
                graph, CONFIG.model_copy(update={"max_messages": 5})
            )

        assert (counts.messages, counts.beyond_ceiling) == (5, 28)

    def test_an_uncapped_rebuild_leaves_nothing_beyond_it(
        self, archived: GraphConfig
    ) -> None:
        """And asks the archive for no total it has no use for."""
        assert _rebuild(archived).beyond_ceiling == 0

    def test_a_ceiling_the_archive_never_reaches_leaves_nothing_out(
        self, archived: GraphConfig
    ) -> None:
        """The number is what the read did not reach, not the ceiling's slack."""
        with client.session(archived) as graph:
            counts = rebuild_derived(
                graph, CONFIG.model_copy(update={"max_messages": 500})
            )

        assert (counts.messages, counts.beyond_ceiling) == (33, 0)

    def test_a_message_with_no_canonical_id_is_not_counted_twice(
        self, archived: GraphConfig
    ) -> None:
        """``unidentified`` already owns it, so the archive's total has to be
        the read's own population rather than every ``Message`` node."""
        with client.session(archived) as graph:
            graph.execute("CREATE (m:Message {subject: 'no canonical id'})")
            counts = rebuild_derived(
                graph, CONFIG.model_copy(update={"max_messages": 5})
            )

        assert (counts.unidentified, counts.beyond_ceiling) == (1, 28)


class TestTheDeleteBatchSize:
    """``DELETE_BATCH`` is ten thousand, so the drain loop never runs twice.

    FalkorDB has no ``CALL … IN TRANSACTIONS``, which is the whole reason the
    delete is batched at all — and against a corpus that produces five derived
    nodes the batching is dead code. Turning it down to two is what makes the
    loop, and its termination, run against a real graph.
    """

    def test_a_layer_removed_two_at_a_time_is_removed_completely(
        self, archived: GraphConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild(archived)
        monkeypatch.setattr(rebuild, "DELETE_BATCH", 2)

        second = _rebuild(archived)

        assert (second.deleted_nodes, second.deleted_edges) == (5, 3)

    def test_the_graph_is_the_same_whichever_batch_size_removed_it(
        self, archived: GraphConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A drain that stopped a batch early would leave a stale node behind
        and the snapshot would say so."""
        _rebuild(archived)
        with client.session(archived) as graph:
            before = _snapshot(graph)

        monkeypatch.setattr(rebuild, "DELETE_BATCH", 2)
        _rebuild(archived)

        with client.session(archived) as graph:
            assert _snapshot(graph) == before


class TestWhatTheArchiveMayHoldAndTheWriterCannotProduce:
    """A graph that has been around, and a rebuild that has to survive it."""

    def test_one_naive_timestamp_does_not_take_the_rebuild_down(
        self, archived: GraphConfig
    ) -> None:
        """Every date the analyses compare comes through the reader, so one
        value stored without an offset would otherwise make ``min`` over a
        group's dates raise and end the whole run.

        Two messages sharing a subject, so the naive one really does reach a
        comparison rather than sitting in a component of its own.
        """
        with client.session(archived) as graph:
            for key, sent_at in (
                ("naive-a", "2026-02-01T08:00:00"),
                ("naive-b", "2026-03-01T08:00:00+00:00"),
            ):
                graph.execute(
                    "CREATE (m:Message {id: $id, sent_at: $sent_at, simhash: 0, "
                    "refs: [], subject_norm: 'wartungsfenster', "
                    "participant_key: 'k-naive'})",
                    {"id": f"{key}@nordlicht.example", "sent_at": sent_at},
                )

        counts = _rebuild(archived)

        assert counts.messages == 35
        assert counts.topics == 2


class TestIdempotence:
    """Twice over an unchanged archive writes the same graph, byte for byte."""

    def test_the_second_run_reports_the_same_findings(
        self, archived: GraphConfig
    ) -> None:
        first = _rebuild(archived)

        second = _rebuild(archived)

        assert second.model_dump(exclude={"deleted_nodes", "deleted_edges"}) == (
            first.model_dump(exclude={"deleted_nodes", "deleted_edges"})
        )

    def test_the_second_run_deletes_exactly_what_the_first_one_wrote(
        self, archived: GraphConfig
    ) -> None:
        """Two groups, one topic and two templates, and three pairs — which is
        also the proof that the delete really reaches everything the write
        produced."""
        _rebuild(archived)

        second = _rebuild(archived)

        assert (second.deleted_nodes, second.deleted_edges) == (5, 3)

    def test_every_node_id_edge_and_property_is_identical(
        self, archived: GraphConfig
    ) -> None:
        """The strong form of the contract, and the reason topic ids are a
        digest of their members rather than a ULID: a random key would make
        this assertion fail on a graph nobody changed."""
        _rebuild(archived)
        with client.session(archived) as graph:
            before = _snapshot(graph)

        _rebuild(archived)

        with client.session(archived) as graph:
            assert _snapshot(graph) == before

    def test_it_holds_over_a_graph_that_started_dirty(
        self, archived: GraphConfig
    ) -> None:
        """The first run cleans up; the second has to agree with it anyway."""
        with client.session(archived) as graph:
            _dirty(graph)
        _rebuild(archived)
        with client.session(archived) as graph:
            before = _snapshot(graph)

        _rebuild(archived)

        with client.session(archived) as graph:
            assert _snapshot(graph) == before

    def test_the_ground_truth_is_the_same_after_both_runs(
        self, archived: GraphConfig
    ) -> None:
        """Message, Address, Thread, Label, Attachment, Account and every edge
        the import wrote — none of it is the derived layer's to change."""
        with client.session(archived) as graph:
            before = ground_truth(graph)

        _rebuild(archived)
        _rebuild(archived)

        with client.session(archived) as graph:
            assert ground_truth(graph) == before


class TestTheProgressHook:
    """A job row has to be able to move while a rebuild runs."""

    def test_every_stage_reports_once_and_in_order(self, archived: GraphConfig) -> None:
        seen: list[RebuildProgress] = []

        with client.session(archived) as graph:
            rebuild_derived(graph, CONFIG, on_progress=seen.append)

        assert [one.stage for one in seen] == [
            RebuildStage.DELETE,
            RebuildStage.READ,
            RebuildStage.CORRESPONDENTS,
            RebuildStage.TOPICS,
            RebuildStage.TEMPLATES,
        ]

    def test_each_stage_reports_what_it_produced(self, archived: GraphConfig) -> None:
        seen: list[RebuildProgress] = []

        with client.session(archived) as graph:
            rebuild_derived(graph, CONFIG, on_progress=seen.append)

        done = {one.stage: one.done for one in seen}
        assert done[RebuildStage.READ] == 33
        assert done[RebuildStage.CORRESPONDENTS] == 2
        assert done[RebuildStage.TOPICS] == 1
        assert done[RebuildStage.TEMPLATES] == 2

    def test_a_rebuild_without_a_hook_is_the_same_rebuild(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            rebuild_derived(graph, CONFIG, on_progress=lambda _one: None)
            with_hook = _snapshot(graph)

        _rebuild(archived)

        with client.session(archived) as graph:
            assert _snapshot(graph) == with_hook


def test_an_empty_archive_rebuilds_to_nothing(config: GraphConfig) -> None:
    """No messages, no findings, no failure — the state a fresh install is in."""
    counts = _rebuild(config)

    assert counts == DerivedCounts()
