"""A2 over the planted corpus — one project, and nothing else pretending to be.

The corpus holds one piece of work and five ways of looking like one. Five
messages share a ticket token; twelve share a participant group and a month;
ten share a participant group and an issue number; two share a company footer;
two share a hidden recipient; two share a participant group and nothing at all.
A2 has to return **exactly one** topic, and every one of those other blocks is
a way for it to return more.

The calibration that makes that come out right is the split in
:data:`~mailarc_analytics.derived.model.SIGNAL_WEIGHTS` around
``topic_min_score``: a ticket, a thread or a subject carries a topic alone,
while a shared attachment and a shared participant group carry one only
together. So the negative cases are not "the algorithm happened not to join
them" — they are the threshold doing what it was set to do, and each is
asserted against the weight that decides it.

Still no embedder anywhere, and that is now a claim with a price on it. Signal
6 arrives through ``extra_edges``, computed by a caller that holds one, so a
rebuild without an embedder takes the same path it took in phase 5 — and every
expectation in this file is a phase-5 expectation, which is why
:data:`PHASE_FIVE` pins the whole answer literally rather than trusting six
separate assertions to notice a drift.
"""

from typing import Any

import corpus
import pytest

from mailarc_analytics import (
    SIGNAL_WEIGHTS,
    MessageFacts,
    SimilarityEdge,
    TopicFindings,
    TopicSignal,
    build_topics,
    topic_id,
)

CONFIG = corpus.calibrated_config()

PROJECT = tuple(corpus.canonical(f"p{n}") for n in (1, 2, 3, 4, 5))
"""The only five messages that may end up in a topic."""

EMBEDDING = "embedding"
"""The ``method`` signal 6 writes. A string here for the same reason it is one
on the edge: :class:`~mailarc_analytics.derived.model.TopicSignal` is the
vocabulary of the five *exact* signals, and A2 must go on clustering correctly
under a method name it has no weight for."""

PHASE_FIVE = {
    "clusters": [
        {
            "id": "topic:8ddcd22af04394667b0b8bfef1d1a97e",
            "label": "angebot datenmigration",
            "method": "ref",
            "score": 1.0,
            "first_seen": "2026-01-12T09:00:00Z",
            "last_seen": "2026-02-10T16:00:00Z",
            "members": [
                {"message_id": one, "score": 1.0, "method": "ref"} for one in PROJECT
            ],
        }
    ],
    "dropped_buckets": 0,
    "dropped_weak_pairs": 0,
}
"""A2's answer over the planted corpus, measured before signal 6 existed.

Copied out of a run of the phase-5 code, field for field, and it is the only
form of evidence that carries here: "the analyses run unchanged without an
embedder" is a claim about *bytes*, and a test that re-asserted the cluster
count would pass just as happily if a score, a label or a method had moved.
Every other expectation in this file is a reading of one line of this dict.
"""


def _fact(key: str, **fields: Any) -> MessageFacts:
    """One hand-built message, for the signal claims the corpus cannot plant.

    A bucket of two hundred and one messages and a weak-pair budget of three
    are properties of the algorithm, not of an archive; putting them in the
    corpus would make it unreadable and would still not exercise them exactly.
    """
    return MessageFacts(id=key, **fields)


@pytest.fixture(scope="module")
def found() -> TopicFindings:
    """A2 over the whole planted corpus, with no extra edges."""
    return build_topics(corpus.planted_facts(), CONFIG)


class TestTheOneTopicThatWasPlanted:
    """Exact count, exact membership, exact reason."""

    def test_the_corpus_yields_one_topic(self, found: TopicFindings) -> None:
        assert len(found.clusters) == 1

    def test_it_holds_exactly_the_five_project_messages(
        self, found: TopicFindings
    ) -> None:
        """Including the two that carry the ticket in the body alone.

        ``p3`` and ``p5`` have unrelated subjects, no thread and no attachment.
        They are here because ``refs`` is read from the full body, and a reader
        that took it from anywhere narrower would report a topic of three.
        """
        assert tuple(one.message_id for one in found.clusters[0].members) == PROJECT

    def test_its_id_is_the_digest_of_its_members(self, found: TopicFindings) -> None:
        """Which is what makes a rebuild a no-op instead of a rewrite."""
        assert found.clusters[0].id == topic_id(PROJECT)
        assert found.clusters[0].id == "topic:8ddcd22af04394667b0b8bfef1d1a97e"

    def test_it_reads_as_a_fact_rather_than_a_suggestion(
        self, found: TopicFindings
    ) -> None:
        """``method="ref"`` is what §6.2 calls a *Tatsache*.

        The score is on the edges as well as on the node, because a message
        pulled in by a ticket token must not lend its confidence to one pulled
        in by a shared attachment.
        """
        topic = found.clusters[0]

        assert (topic.method, topic.score) == (TopicSignal.REF, 1.0)
        assert {(one.score, one.method) for one in topic.members} == {(1.0, "ref")}

    def test_it_is_named_after_the_subject_its_messages_most_often_carry(
        self, found: TopicFindings
    ) -> None:
        assert found.clusters[0].label == "angebot datenmigration"

    def test_nothing_was_dropped_for_being_boilerplate_or_for_want_of_memory(
        self, found: TopicFindings
    ) -> None:
        """A corpus of thirty-three cannot hit either guard; if it did, the
        counts above would be measuring the guard rather than the analysis."""
        assert (found.dropped_buckets, found.dropped_weak_pairs) == (0, 0)


class TestTheNegativeControls:
    """Four blocks that share something, and must still stay apart."""

    def test_no_message_outside_the_project_block_is_in_any_topic(
        self, found: TopicFindings
    ) -> None:
        clustered = {
            one.message_id for topic in found.clusters for one in topic.members
        }

        assert clustered == set(PROJECT)

    @pytest.mark.parametrize("key", ["w1", "w2", "f1", "f2", "b1", "b2"])
    def test_the_planted_negatives_are_named_individually(
        self, found: TopicFindings, key: str
    ) -> None:
        """So a failure says which control broke rather than "one too many"."""
        clustered = {
            one.message_id for topic in found.clusters for one in topic.members
        }

        assert corpus.canonical(key) not in clustered

    def test_a_shared_participant_group_alone_never_joins_anything(self) -> None:
        """The weakest signal, at 0.2 against a threshold of 0.5.

        This is what keeps twelve monthly status reports to one recipient from
        becoming a "project" — and the corpus contains exactly that trap twice.
        """
        facts = [_fact(f"m{n}", participant_key="circle") for n in range(4)]

        assert SIGNAL_WEIGHTS[TopicSignal.PARTICIPANTS] < CONFIG.topic_min_score
        assert build_topics(facts, CONFIG).clusters == ()

    def test_a_shared_attachment_alone_never_joins_anything(self) -> None:
        """0.4, still short of the threshold: a file sent to two unrelated
        people is a file, not a project."""
        facts = [_fact(f"m{n}", attachments=("sha",)) for n in range(3)]

        assert SIGNAL_WEIGHTS[TopicSignal.ATTACHMENT] < CONFIG.topic_min_score
        assert build_topics(facts, CONFIG).clusters == ()

    def test_the_two_weak_signals_together_do_join(self) -> None:
        """0.4 plus 0.2 clears 0.5 — the combination the split was set for.

        The edge is attributed to the stronger of the two, so a reader sees
        "attachment" rather than the weakest thing that contributed.
        """
        facts = [
            _fact(f"m{n}", attachments=("sha",), participant_key="circle")
            for n in range(3)
        ]

        clusters = build_topics(facts, CONFIG).clusters

        assert len(clusters) == 1
        assert clusters[0].method == TopicSignal.ATTACHMENT
        assert clusters[0].score == 0.6


class TestTheStrongSignals:
    """Each clears the threshold on its own, and each says so on the edge."""

    @pytest.mark.parametrize(
        ("signal", "fields"),
        [
            (TopicSignal.REF, {"refs": ("PROJ-1",)}),
            (TopicSignal.THREAD, {"thread_id": "1:t"}),
            (TopicSignal.SUBJECT, {"subject_norm": "angebot"}),
        ],
    )
    def test_one_occurrence_is_already_a_topic(
        self, signal: TopicSignal, fields: dict[str, Any]
    ) -> None:
        facts = [_fact(f"m{n}", **fields) for n in range(2)]

        clusters = build_topics(facts, CONFIG).clusters

        assert SIGNAL_WEIGHTS[signal] >= CONFIG.topic_min_score
        assert len(clusters) == 1
        assert clusters[0].method == signal
        assert clusters[0].score == SIGNAL_WEIGHTS[signal]

    def test_an_empty_subject_is_not_a_shared_subject(self) -> None:
        """Otherwise every message the parser could not read a subject off
        would be one topic."""
        facts = [_fact(f"m{n}", subject_norm="") for n in range(3)]

        assert build_topics(facts, CONFIG).clusters == ()

    def test_the_strongest_signal_in_a_component_names_it(self) -> None:
        """A component held together by a ticket and a subject is a ticket
        cluster; reading it as a subject cluster would understate it."""
        facts = [
            _fact("m1", refs=("PROJ-1",), subject_norm="angebot"),
            _fact("m2", refs=("PROJ-1",), subject_norm="angebot"),
            _fact("m3", subject_norm="angebot"),
        ]

        clusters = build_topics(facts, CONFIG).clusters

        assert len(clusters) == 1
        assert clusters[0].method == TopicSignal.REF


class TestTheLabel:
    """What a human will call the topic, and why it may not wobble."""

    def test_it_is_the_most_common_non_empty_subject(self) -> None:
        facts = [
            _fact("m1", refs=("PROJ-1",), subject_norm="angebot"),
            _fact("m2", refs=("PROJ-1",), subject_norm="angebot"),
            _fact("m3", refs=("PROJ-1",), subject_norm="rueckfrage"),
        ]

        assert build_topics(facts, CONFIG).clusters[0].label == "angebot"

    def test_a_tie_goes_to_the_smallest_subject_and_not_to_insertion_order(
        self,
    ) -> None:
        """``Counter.most_common`` is insertion-ordered, so a rebuild would
        rename a topic it had not changed — a data change that is not one."""
        forwards = [
            _fact("m1", refs=("PROJ-1",), subject_norm="zweite"),
            _fact("m2", refs=("PROJ-1",), subject_norm="erste"),
        ]
        backwards = list(reversed(forwards))

        assert build_topics(forwards, CONFIG).clusters[0].label == "erste"
        assert build_topics(backwards, CONFIG).clusters[0].label == "erste"

    def test_it_falls_back_to_the_shared_ticket_when_no_subject_is_shared(
        self,
    ) -> None:
        """Five mails about one ticket usually normalise to five subjects."""
        facts = [
            _fact("m1", refs=("PROJ-1",), subject_norm=""),
            _fact("m2", refs=("PROJ-1",), subject_norm=""),
        ]

        assert build_topics(facts, CONFIG).clusters[0].label == "PROJ-1"

    def test_it_is_empty_when_the_members_share_neither(self) -> None:
        """Better an unnamed topic than one named after one arbitrary member."""
        facts = [
            _fact("m1", attachments=("sha",), participant_key="circle"),
            _fact("m2", attachments=("sha",), participant_key="circle"),
        ]

        assert build_topics(facts, CONFIG).clusters[0].label == ""


class TestTheGuards:
    """Boilerplate and memory, both counted rather than swallowed."""

    def test_a_bucket_over_the_cap_is_treated_as_boilerplate(self) -> None:
        """A subject shared by thousands is not a project.

        Joining them would produce one topic holding a fifth of the archive,
        which is why the cap drops the bucket for *every* signal rather than
        only for the ones that need a pair table.
        """
        facts = [
            _fact(f"m{n:04d}", subject_norm="rechnung")
            for n in range(CONFIG.topic_bucket_cap + 1)
        ]

        found = build_topics(facts, CONFIG)

        assert found.clusters == ()
        assert found.dropped_buckets == 1

    def test_a_dropped_bucket_never_writes_its_key_into_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """For ``TopicSignal.SUBJECT`` the key is somebody's mail subject.

        AGENTS §7 keeps user content out of logs a person might share, and
        there is one such line per dropped bucket — an archive full of
        boilerplate would write a page of real correspondence. Nothing is lost
        by dropping the key: the count reaches the caller as
        ``dropped_buckets`` and both ``build_topics`` and ``app/derive.py``
        already log it once per run.
        """
        subject = "rechnung nr 4711 kunde meier gmbh"
        facts = [
            _fact(f"m{n:04d}", subject_norm=subject)
            for n in range(CONFIG.topic_bucket_cap + 1)
        ]

        with caplog.at_level("DEBUG"):
            found = build_topics(facts, CONFIG)

        assert found.dropped_buckets == 1
        assert subject not in caplog.text
        assert "meier" not in caplog.text

    def test_a_dropped_bucket_is_a_debug_line_and_not_an_information_one(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§1 rule 9 makes ``debug`` the default and reserves ``info`` for
        events worth a reader's attention. One line per bucket is not one; the
        run's own summary is."""
        facts = [
            _fact(f"m{n:04d}", subject_norm="rechnung")
            for n in range(CONFIG.topic_bucket_cap + 1)
        ]

        with caplog.at_level("INFO"):
            build_topics(facts, CONFIG)

        assert "boilerplate" not in caplog.text

    def test_a_subject_bucket_the_weak_table_could_never_hold_is_still_a_topic(
        self,
    ) -> None:
        """The subject signal's weight *is* ``topic_min_score``, to the digit.

        Which side of the split it falls on is decided by one ``>=`` in
        ``_signals``, and the difference is not academic: a strong signal
        chains its bucket in *k* unions and never enters a pair table, while a
        weak one costs ``k(k-1)/2`` entries and degrades when the budget runs
        out. Twenty messages against a budget of ten is that difference — on
        the wrong side of the comparison this is zero topics and a hundred and
        ninety dropped pairs, which on a real archive is every ticket-system
        notification subject.
        """
        facts = [_fact(f"m{n:04d}", subject_norm="wartungsfenster") for n in range(20)]
        stingy = CONFIG.model_copy(update={"topic_max_weak_pairs": 10})

        found = build_topics(facts, stingy)

        assert SIGNAL_WEIGHTS[TopicSignal.SUBJECT] == CONFIG.topic_min_score
        assert found.dropped_weak_pairs == 0
        assert [one.message_count for one in found.clusters] == [20]

    def test_a_bucket_exactly_at_the_cap_still_becomes_a_topic(self) -> None:
        """The cap is a ceiling, not a fence one short of it."""
        facts = [
            _fact(f"m{n:04d}", subject_norm="rechnung")
            for n in range(CONFIG.topic_bucket_cap)
        ]

        found = build_topics(facts, CONFIG)

        assert found.dropped_buckets == 0
        assert found.clusters[0].message_count == CONFIG.topic_bucket_cap

    def test_the_weak_pair_budget_takes_the_smallest_buckets_first(self) -> None:
        """A four-message shared-attachment bucket is a project; a large
        participant bucket is a distribution list, and that is the right
        triage when only one of them fits.

        Three pairs of budget: the three-member bucket costs three and is
        scored, the four-member one costs six and is refused, and the six it
        would have cost is reported rather than lost.
        """
        small = [_fact(f"s{n}", attachments=("small",)) for n in range(3)]
        large = [_fact(f"l{n}", attachments=("large",)) for n in range(4)]

        found = build_topics(
            [*small, *large], CONFIG.model_copy(update={"topic_max_weak_pairs": 3})
        )

        assert found.dropped_weak_pairs == 6

    def test_no_strong_signal_is_ever_dropped_for_want_of_memory(self) -> None:
        """The correct failure direction: §6.2 calls a ticket cluster a fact
        and everything else a suggestion, so only suggestions may degrade."""
        facts = [
            _fact(f"m{n}", refs=("PROJ-1",), attachments=("sha",)) for n in range(4)
        ]

        found = build_topics(
            facts, CONFIG.model_copy(update={"topic_max_weak_pairs": 0})
        )

        assert len(found.clusters) == 1
        assert found.clusters[0].method == TopicSignal.REF


class TestWithNoEmbedderNothingMoved:
    """Phase 6's definition of done: A1-A3 run unchanged without an embedder.

    §7.4 makes ``provider=none`` the default and the supported state, not a
    degraded one, so the *absence* of signal 6 is the configuration nearly
    every archive will run in. Which makes this the equivalence that has to be
    proven rather than assumed: every topic assertion in this file, in
    ``test_derived_rebuild*.py`` and in ``test_queries_reports_local.py`` is
    read against a corpus that has no vectors.
    """

    def test_the_findings_are_byte_for_byte_what_phase_five_measured(
        self, found: TopicFindings
    ) -> None:
        """The whole answer, serialised, against a literal from before.

        Compared as a dump rather than as an object so that a failure prints
        the field that moved. Ids, labels, methods, scores, dates, membership
        order and both counters are in scope — which is the point: signal 6
        touches the ranking of a membership's reason, and a ranking change is
        exactly the kind of drift a count-and-method assertion sails past.
        """
        assert found.model_dump(mode="json") == PHASE_FIVE

    def test_an_empty_edge_sequence_is_the_same_as_no_edge_argument(
        self, found: TopicFindings
    ) -> None:
        """Or the parameter would be doing something on its own."""
        assert build_topics(corpus.planted_facts(), CONFIG, extra_edges=()) == found

    def test_the_answer_still_does_not_depend_on_the_order_of_the_facts(self) -> None:
        """Two rebuilds read the same messages; they write the same topic."""
        facts = corpus.planted_facts()

        assert build_topics(tuple(reversed(facts)), CONFIG) == build_topics(
            facts, CONFIG
        )

    def test_two_exact_signals_are_still_ranked_by_score_and_not_by_weight(
        self,
    ) -> None:
        """The case the corpus cannot show, and the one signal 6 could break.

        These two messages share a subject (0.5 on its own) *and* an attachment
        with a participant group (0.4 + 0.2 = 0.6 together), so the stronger
        *signal* and the stronger *reason* are two different answers. Keeping
        the exact five ranked by accumulated score is what phase 5 does; the
        obvious way to demote a suggestion — rank everything by the method's
        calibrated weight instead — would silently flip this edge to
        ``subject`` and change a graph no embedder was ever involved in.
        """
        facts = [
            _fact(key, subject_norm="x", attachments=("sha",), participant_key="circle")
            for key in ("m1", "m2")
        ]

        topic = build_topics(facts, CONFIG).clusters[0]

        assert topic.method == TopicSignal.ATTACHMENT
        assert {one.method for one in topic.members} == {TopicSignal.ATTACHMENT}
        assert topic.score == 0.6


class TestSignalSix:
    """``extra_edges`` — what a KNN hands in, and what A2 does with it."""

    def test_an_extra_edge_joins_what_the_exact_signals_left_open(self) -> None:
        facts = [_fact("m1"), _fact("m2")]
        edge = SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=0.9)

        clusters = build_topics(facts, CONFIG, extra_edges=[edge]).clusters

        assert len(clusters) == 1
        assert clusters[0].method == EMBEDDING
        assert clusters[0].score == 0.9

    def test_a_component_only_a_suggestion_built_is_reported_as_a_suggestion(
        self,
    ) -> None:
        """§6.2's whole distinction, and the UI renders ``method`` as a badge.

        On the node *and* on every edge, because the two are read in different
        places: the badge comes off the node, and a user asking why one
        particular message is in the topic reads the edge.
        """
        facts = [_fact("m1"), _fact("m2"), _fact("m3")]
        edges = [
            SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=0.91),
            SimilarityEdge(left="m2", right="m3", method=EMBEDDING, weight=0.88),
        ]

        topic = build_topics(facts, CONFIG, extra_edges=edges).clusters[0]

        assert topic.method == EMBEDDING
        assert {one.method for one in topic.members} == {EMBEDDING}
        assert [one.score for one in topic.members] == [0.91, 0.91, 0.88]

    def test_the_similarity_is_what_lands_on_the_edge_as_a_score(self) -> None:
        """Not the method's weight. §6.2 asks for the similarity itself, and a
        user throwing away a wrong topic wants to see how near the two texts
        were, not a constant this module chose."""
        edge = SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=0.837)

        topic = build_topics(
            [_fact("m1"), _fact("m2")], CONFIG, extra_edges=[edge]
        ).clusters[0]

        assert {one.score for one in topic.members} == {0.837}

    @pytest.mark.parametrize(
        ("weight", "joined"),
        [
            (CONFIG.topic_min_score - 0.1, False),
            (CONFIG.topic_min_score, True),
            (CONFIG.topic_min_score + 0.1, True),
        ],
    )
    def test_a_suggestion_clears_the_same_bar_a_fact_does_and_at_the_same_place(
        self, weight: float, joined: bool
    ) -> None:
        """Exactly at the threshold is the case that matters.

        Signal 6 has a cosine cut-off of its own — ``topic_similarity_min``,
        0.82, applied by the caller that runs the KNN — and this floor sits
        under it: a comparison one notch out here would refuse an edge at
        precisely its own configured threshold and connect nothing. Below and
        above are the easy halves; the middle row is the one the seam exists
        for.
        """
        edge = SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=weight)

        clusters = build_topics(
            [_fact("m1"), _fact("m2")], CONFIG, extra_edges=[edge]
        ).clusters

        assert bool(clusters) is joined

    def test_an_extra_edge_naming_a_message_this_run_never_read_is_ignored(
        self,
    ) -> None:
        """A nearest-neighbour search over a stale index must not invent a
        member, and must not take the rebuild down either."""
        edge = SimilarityEdge(left="m1", right="gone", method=EMBEDDING, weight=0.9)

        assert build_topics([_fact("m1")], CONFIG, extra_edges=[edge]).clusters == ()

    def test_a_message_that_is_its_own_nearest_neighbour_joins_nothing(self) -> None:
        """FalkorDB's KNN answers with the query vector's own node first.

        A caller is expected to drop it, and a caller that forgets must not
        make a message a member of a topic with itself.
        """
        edge = SimilarityEdge(left="m1", right="m1", method=EMBEDDING, weight=1.0)

        found = build_topics([_fact("m1")], CONFIG, extra_edges=[edge])

        assert found.clusters == ()

    def test_a_self_pair_does_not_spend_a_slot_of_the_budget(self) -> None:
        """The other half of the sentence above, and the half the default
        ceiling cannot see: one extra pair is invisible against two million,
        so the guard could be deleted and every assertion still held. Asked at
        a ceiling of zero, a wasted slot becomes a refusal that shows.

        It is not a hypothetical either. The KNN returns the query node for
        *every* message, so at production scale a missing guard wastes one
        budget slot per message in the archive.
        """
        edge = SimilarityEdge(left="m1", right="m1", method=EMBEDDING, weight=1.0)
        broke = CONFIG.model_copy(update={"topic_max_weak_pairs": 0})

        found = build_topics([_fact("m1")], broke, extra_edges=[edge])

        assert (found.clusters, found.dropped_weak_pairs) == ((), 0)

    def test_a_mutual_pair_spends_one_slot_and_not_two(self) -> None:
        """The mirror case: a KNN is symmetric, so ``(a, b)`` and ``(b, a)``
        both arrive. Folded to one key they cost one slot; unfolded they cost
        two, and the pair table reports twice what the rebuild applied."""
        edges = [
            SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=0.9),
            SimilarityEdge(left="m2", right="m1", method=EMBEDDING, weight=0.9),
        ]
        one_slot = CONFIG.model_copy(update={"topic_max_weak_pairs": 1})

        found = build_topics([_fact("m1"), _fact("m2")], one_slot, extra_edges=edges)

        assert found.dropped_weak_pairs == 0, "one edge, offered twice, is one pair"
        assert len(found.clusters) == 1

    def test_the_order_the_edges_arrive_in_changes_nothing(self) -> None:
        """A KNN reshuffling itself must not rename a topic."""
        facts = [_fact("m1"), _fact("m2"), _fact("m3")]
        edges = [
            SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=0.9),
            SimilarityEdge(left="m2", right="m3", method=EMBEDDING, weight=0.9),
        ]

        assert build_topics(facts, CONFIG, extra_edges=edges) == build_topics(
            facts, CONFIG, extra_edges=list(reversed(edges))
        )


class TestASuggestionNeverOutranksAFact:
    """A2 may be *widened* by an embedding edge; it may not be *relabelled*.

    The trap is arithmetical rather than conceptual. A cosine similarity lands
    between 0.82 and 1.0, a shared subject is worth 0.5 and a shared attachment
    plus a shared participant group 0.6 — so on the numbers alone the weakest
    of the six signals outscores three of the five exact ones on every edge it
    touches, and ``ABOUT.method`` would report a suggestion where §6.2 promises
    a fact.
    """

    def test_a_pair_a_fact_already_joined_keeps_the_fact_on_its_edge(self) -> None:
        """0.5 from a shared subject beats 0.95 from a KNN, and must."""
        facts = [
            _fact("m1", subject_norm="angebot"),
            _fact("m2", subject_norm="angebot"),
        ]
        edge = SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=0.95)

        topic = build_topics(facts, CONFIG, extra_edges=[edge]).clusters[0]

        assert topic.method == TopicSignal.SUBJECT
        assert {(one.score, one.method) for one in topic.members} == {(0.5, "subject")}

    @pytest.mark.parametrize(
        ("method", "fields"),
        [
            (TopicSignal.REF, {"refs": ("PROJ-1",)}),
            (TopicSignal.THREAD, {"thread_id": "1:t"}),
            (TopicSignal.SUBJECT, {"subject_norm": "angebot"}),
            (
                TopicSignal.ATTACHMENT,
                {"attachments": ("sha",), "participant_key": "circle"},
            ),
        ],
    )
    def test_no_exact_signal_is_ever_overwritten_by_a_suggestion(
        self, method: TopicSignal, fields: dict[str, Any]
    ) -> None:
        """Named one at a time, including the tie at 1.0 against ``ref``.

        A ranking that compared scores alone would lose ``ref`` to a perfect
        cosine match on the alphabet — ``"embedding" < "ref"`` — which is the
        strongest fact in the analysis being demoted by a string comparison.
        """
        facts = [_fact("m1", **fields), _fact("m2", **fields)]
        edge = SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=1.0)

        topic = build_topics(facts, CONFIG, extra_edges=[edge]).clusters[0]

        assert topic.method == method
        assert {one.method for one in topic.members} == {method}

    def test_an_exact_signal_still_names_a_component_an_extra_edge_widened(
        self,
    ) -> None:
        """Signal 6 connects only what 1-5 left open, and does not get to
        relabel what they decided."""
        facts = [
            _fact("m1", refs=("PROJ-1",)),
            _fact("m2", refs=("PROJ-1",)),
            _fact("m3"),
        ]
        edge = SimilarityEdge(left="m2", right="m3", method=EMBEDDING, weight=0.9)

        clusters = build_topics(facts, CONFIG, extra_edges=[edge]).clusters

        assert clusters[0].message_count == 3
        assert clusters[0].method == TopicSignal.REF

    def test_the_message_the_suggestion_brought_in_still_says_so(self) -> None:
        """The node reads as a fact, the one borrowed edge reads as a guess.

        Which is the whole reason ``method`` sits on the edge as well as on the
        node: a five-message ticket topic with one embedding member is not a
        five-message ticket topic, and only the edges can say so.
        """
        facts = [
            _fact("m1", refs=("PROJ-1",)),
            _fact("m2", refs=("PROJ-1",)),
            _fact("m3"),
        ]
        edge = SimilarityEdge(left="m2", right="m3", method=EMBEDDING, weight=0.9)

        topic = build_topics(facts, CONFIG, extra_edges=[edge]).clusters[0]

        assert {one.message_id: one.method for one in topic.members} == {
            "m1": "ref",
            "m2": "ref",
            "m3": EMBEDDING,
        }


class TestThePairBudgetWithSignalSixInPlay:
    """A KNN hands in ``k`` neighbours per message; the ceiling is one ceiling.

    At a hundred thousand messages and ``k=10`` that is a million rows offered
    in one call — more pairs than the exact signals produce in any archive, and
    all of them suggestions. So signal 6 spends the same
    ``topic_max_weak_pairs`` the weak table spends, after it and never instead
    of it, and what does not fit is counted into ``dropped_weak_pairs`` rather
    than quietly left out. Facts are never in that arithmetic at all.
    """

    def test_what_the_weak_table_spent_is_not_available_to_the_suggestions(
        self,
    ) -> None:
        """Four messages on one attachment fill six of a ceiling of seven.

        One pair of room is left, two suggestions are offered, and the second
        is refused — which is the whole claim: one rebuild may not hold twice
        what the setting says, and a second ceiling under the same name would
        let it.
        """
        weak = [_fact(f"w{n}", attachments=("sha",)) for n in range(4)]
        loose = [_fact(f"e{n}") for n in range(4)]
        edges = [
            SimilarityEdge(left="e0", right="e1", method=EMBEDDING, weight=0.9),
            SimilarityEdge(left="e2", right="e3", method=EMBEDDING, weight=0.8),
        ]
        seven = CONFIG.model_copy(update={"topic_max_weak_pairs": 7})

        found = build_topics([*weak, *loose], seven, extra_edges=edges)

        assert found.dropped_weak_pairs == 1
        assert [
            tuple(one.message_id for one in topic.members) for topic in found.clusters
        ] == [("e0", "e1")]

    def test_a_spent_budget_keeps_the_strongest_suggestions(self) -> None:
        """The triage the weak table makes by bucket size, made by similarity.

        A KNN's rows are not equal: 0.95 is two mails about one delivery and
        0.6 is two mails in German. Cutting by arrival order would let the
        order a nearest-neighbour search happened to answer in decide which
        topics exist — the same non-determinism ``_join_extra`` sorts away.
        """
        facts = [_fact(f"m{n}") for n in range(4)]
        edges = [
            SimilarityEdge(left="m0", right="m1", method=EMBEDDING, weight=0.6),
            SimilarityEdge(left="m2", right="m3", method=EMBEDDING, weight=0.95),
        ]
        one_pair = CONFIG.model_copy(update={"topic_max_weak_pairs": 1})

        found = build_topics(facts, one_pair, extra_edges=edges)

        assert found.dropped_weak_pairs == 1
        assert [
            tuple(one.message_id for one in topic.members) for topic in found.clusters
        ] == [("m2", "m3")]

    def test_a_fact_is_never_refused_to_make_room_for_a_suggestion(self) -> None:
        """§6.2's failure direction: memory pressure may not cost a fact.

        A ceiling of zero, a ticket cluster and a suggestion. The ticket
        cluster is built without ever entering a table, the suggestion is
        refused, and the refusal is a number the job row can show.
        """
        facts = [
            _fact("m1", refs=("PROJ-1",)),
            _fact("m2", refs=("PROJ-1",)),
            _fact("m3"),
            _fact("m4"),
        ]
        edges = [SimilarityEdge(left="m3", right="m4", method=EMBEDDING, weight=0.9)]
        nothing = CONFIG.model_copy(update={"topic_max_weak_pairs": 0})

        found = build_topics(facts, nothing, extra_edges=edges)

        assert found.dropped_weak_pairs == 1
        assert [
            tuple(one.message_id for one in topic.members) for topic in found.clusters
        ] == [("m1", "m2")]

    def test_a_suggestion_below_the_threshold_costs_no_budget(self) -> None:
        """It was never a pair, so refusing it is not a drop worth counting.

        The counter has to mean "this run saw less than the archive holds". A
        row the gate rejected is a row the analysis *decided* about, and
        counting it would make every KNN look like a memory problem.
        """
        edge = SimilarityEdge(
            left="m1", right="m2", method=EMBEDDING, weight=CONFIG.topic_min_score - 0.1
        )
        nothing = CONFIG.model_copy(update={"topic_max_weak_pairs": 0})

        found = build_topics([_fact("m1"), _fact("m2")], nothing, extra_edges=[edge])

        assert (found.clusters, found.dropped_weak_pairs) == ((), 0)

    def test_a_mutual_neighbour_pair_is_charged_once(self) -> None:
        """``a`` is in ``b``'s ten nearest and ``b`` is in ``a``'s.

        A KNN over ``n`` messages asks per message, so a genuine pair arrives
        twice with its endpoints the other way round. Folding the two into one
        is what makes the ceiling count pairs the way the weak table counts
        them — otherwise a budget of a million would refuse a rebuild that had
        found half that many pairs.
        """
        facts = [_fact("m1"), _fact("m2")]
        edges = [
            SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=0.9),
            SimilarityEdge(left="m2", right="m1", method=EMBEDDING, weight=0.9),
        ]
        one_pair = CONFIG.model_copy(update={"topic_max_weak_pairs": 1})

        found = build_topics(facts, one_pair, extra_edges=edges)

        assert found.dropped_weak_pairs == 0
        assert [one.message_count for one in found.clusters] == [2]

    def test_a_refused_suggestion_is_reported_and_not_only_counted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning, like the weak table's, and without a message id in it.

        AGENTS §7 keeps user content out of a log a person might attach to a
        bug report, and a canonical id is an address and a subject line away
        from one.
        """
        facts = [_fact("m1"), _fact("m2")]
        edges = [SimilarityEdge(left="m1", right="m2", method=EMBEDDING, weight=0.9)]
        nothing = CONFIG.model_copy(update={"topic_max_weak_pairs": 0})

        with caplog.at_level("WARNING"):
            build_topics(facts, nothing, extra_edges=edges)

        assert "1 of 1 semantic pairs not applied" in caplog.text
        assert "m1" not in caplog.text


class TestASignalCountsOnceHoweverManyWaysItMatches:
    """A weak signal contributes its own weight to a pair, not a multiple of it.

    ``SIGNAL_WEIGHTS`` and ``topic_min_score`` are calibrated against each
    other so that no single weak signal joins a pair on its own: attachment is
    0.4 against a threshold of 0.5. But the buckets for that signal are keyed by
    attachment *hash*, so two messages that share two files land in two buckets,
    and accumulating per bucket gave the pair 0.8 — clearing a threshold the
    weight was chosen to sit under.

    Two colleagues who both attach the company logo and the same signature
    image to unrelated mail are the everyday shape of that. The signal is "these
    two share a file", and sharing a second file is more of the same evidence,
    not a second kind of it.
    """

    def test_two_shared_attachments_do_not_add_up_to_a_topic(self) -> None:
        both = ("sha-logo", "sha-signature")
        facts = [
            _fact("unrelated-a", attachments=both, subject_norm="quarterly figures"),
            _fact("unrelated-b", attachments=both, subject_norm="lunch on friday"),
        ]

        found = build_topics(facts, CONFIG)

        assert found.clusters == (), (
            "two shared attachments cleared topic_min_score on their own; the "
            "attachment weight was counted once per file instead of once per signal"
        )

    def test_one_shared_attachment_still_does_not(self) -> None:
        """The calibration this protects, stated as its own case."""
        facts = [
            _fact("a", attachments=("sha-logo",), subject_norm="quarterly figures"),
            _fact("b", attachments=("sha-logo",), subject_norm="lunch on friday"),
        ]

        assert build_topics(facts, CONFIG).clusters == ()

    def test_but_two_different_weak_signals_still_combine(self) -> None:
        """The behaviour that must survive the fix.

        Two *kinds* of evidence are what the accumulation is for: a shared
        attachment (0.4) plus a shared participant group (0.2) reaches 0.6 and
        clears the threshold. Only repetition of the same kind is discounted.
        """
        facts = [
            _fact(
                "c",
                attachments=("sha-plan",),
                participant_key="team-key",
                subject_norm="one thing",
            ),
            _fact(
                "d",
                attachments=("sha-plan",),
                participant_key="team-key",
                subject_norm="another thing",
            ),
        ]

        found = build_topics(facts, CONFIG)

        assert len(found.clusters) == 1
        assert found.clusters[0].method == TopicSignal.ATTACHMENT.value, (
            "the strongest signal that joined the pair names the edge"
        )
