"""The schema and the vocabulary — the parts every analysis agrees on.

Two groups of claims. The deterministic ids, because "``rebuild-derived`` is
idempotent" is a property of those two functions before it is a property of
anything else: a random key would make the delete-and-recompute write a
different graph every run, and no amount of correct clustering would fix it.
The OGM declarations, because runic resolves annotations
at class-declaration time and a forward reference silently strips the
converters off *every* field on a node — a failure that shows up as a
``datetime`` written as a Python object, three layers away from its cause.
"""

from datetime import UTC, datetime
from typing import Any

import corpus
import pytest
from pydantic import BaseModel, ValidationError

from mailarc_analytics import (
    SIGNAL_WEIGHTS,
    About,
    AddressedGroup,
    CoAddressed,
    CoAddressedPair,
    Community,
    CommunityFacts,
    CommunityFindings,
    CorrespondentFindings,
    DerivedCounts,
    Group,
    GroupFacts,
    Grouping,
    GroupingKind,
    ImportanceScore,
    InCircle,
    InstanceOf,
    MemberOf,
    MessageFacts,
    MessageSignals,
    RebuildProgress,
    RebuildStage,
    SimilarityEdge,
    Suggested,
    Suggestion,
    Template,
    TemplateCluster,
    TemplateDirection,
    TemplateGroup,
    TemplateGrouping,
    TemplateMember,
    Topic,
    TopicCluster,
    TopicFindings,
    TopicMember,
    TopicSignal,
    community_id,
    template_id,
    topic_id,
)
from mailarc_core.archive.model import Message, to_signed_64, to_unsigned_64

VALUE_OBJECTS = (
    MessageFacts, CoAddressedPair, GroupFacts, CorrespondentFindings,
    SimilarityEdge, TopicMember, TopicCluster, TopicFindings,
    TemplateMember, TemplateGroup, TemplateGrouping, TemplateCluster,
    RebuildProgress, DerivedCounts,
    MessageSignals, ImportanceScore, CommunityFacts, CommunityFindings,
    Suggestion, Grouping,
)  # fmt: skip

TOP_BIT = 0xA0B86145044638A0
"""A fingerprint with bit 63 set — the shape the graph has to store signed."""


def _fields(entity: Any) -> Any:
    """Everything runic registered on a node or an edge."""
    return entity._fields


def _field(node: Any, name: str) -> Any:
    """One declared field's descriptor, by name.

    ``Any`` on both ends because runic keeps its metadata on private class
    attributes a type checker cannot see. Asking the class rather than reading
    ``model.py`` is the point of this file: what matters is what was actually
    registered.
    """
    return next(one.field for one in _fields(node) if one.name == name)


class TestValueObjects:
    """Frozen pydantic models, never dataclasses — the house rule, checked."""

    @pytest.mark.parametrize("model", VALUE_OBJECTS)
    def test_every_value_object_is_a_frozen_pydantic_model(
        self, model: type[BaseModel]
    ) -> None:
        """Frozen because an analysis passes these between its halves.

        A mutable finding is a finding a later stage can edit, and the write
        half would then store something the compute half never decided.
        """
        assert issubclass(model, BaseModel)
        assert model.model_config.get("frozen") is True

    def test_a_frozen_finding_refuses_to_be_edited(self) -> None:
        pair = CoAddressedPair(left="a@example.com", right="b@example.com", count=2)

        with pytest.raises(ValidationError):
            pair.count = 3  # ty: ignore[invalid-assignment]

    def test_a_cluster_counts_its_own_members(self) -> None:
        """Never a stored field, so the count cannot disagree with the list."""
        topic = TopicCluster(
            id="topic:x",
            members=(
                TopicMember(message_id="a", score=1.0, method="ref"),
                TopicMember(message_id="b", score=1.0, method="ref"),
            ),
        )
        template = TemplateCluster(
            id="template:x:sent",
            direction=TemplateDirection.SENT,
            members=(TemplateMember(message_id="a", distance=0),),
        )

        assert topic.message_count == 2
        assert template.occurrences == 1

    def test_a_grouping_names_its_members_once_and_in_order(self) -> None:
        """What the late body read binds as ``$ids``; duplicates would cost a
        round trip and an unstable order would cost reproducibility."""
        grouping = TemplateGrouping(
            groups=(
                TemplateGroup(
                    direction=TemplateDirection.SENT,
                    representative="b",
                    representative_simhash=1,
                    members=(
                        TemplateMember(message_id="b", distance=0),
                        TemplateMember(message_id="a", distance=2),
                    ),
                ),
                TemplateGroup(
                    direction=TemplateDirection.RECEIVED,
                    representative="a",
                    representative_simhash=2,
                    members=(TemplateMember(message_id="a", distance=0),),
                ),
            )
        )

        assert grouping.member_ids == ("a", "b")


class TestTheSignalVocabulary:
    """Five signals, five weights, and one threshold that splits them."""

    def test_the_weights_are_the_calibration_the_spec_asks_for(self) -> None:
        assert dict(SIGNAL_WEIGHTS) == {
            TopicSignal.REF: 1.0,
            TopicSignal.CONVERSATION: 0.9,
            TopicSignal.THREAD: 0.8,
            TopicSignal.SUBJECT: 0.5,
            TopicSignal.ATTACHMENT: 0.4,
            TopicSignal.PARTICIPANTS: 0.2,
        }

    def test_a_conversation_is_a_fact_and_outranks_a_thread(self) -> None:
        """Union-find over ``thread_id`` *and* the reply parent, so it joins
        what a provider's thread id splits across two accounts.

        Above ``THREAD`` because it is the stronger evidence of the same two
        things: a reply header is the sender's own statement that this message
        answers that one, while a thread id is one provider's grouping of its
        own copy. Below ``REF`` because a ticket token names the piece of work
        and a conversation only names the exchange.
        """
        assert SIGNAL_WEIGHTS[TopicSignal.CONVERSATION] == 0.9
        assert (
            SIGNAL_WEIGHTS[TopicSignal.THREAD]
            < SIGNAL_WEIGHTS[TopicSignal.CONVERSATION]
            < SIGNAL_WEIGHTS[TopicSignal.REF]
        )

    def test_the_weights_cannot_be_edited_at_runtime(self) -> None:
        """A mapping proxy, because the five numbers only mean anything
        relative to each other and to ``topic_min_score``."""
        with pytest.raises(TypeError):
            SIGNAL_WEIGHTS[TopicSignal.REF] = 0.1  # ty: ignore[invalid-assignment]

    def test_the_default_threshold_splits_strong_from_weak(self) -> None:
        """Three signals carry a topic alone; two carry one only together.

        This is the calibration the whole of A2's negative case rests on: a
        shared participant group at 0.2 must not become a project, while a
        shared attachment *and* a shared participant group at 0.6 may.
        """
        threshold = corpus.calibrated_config().topic_min_score
        strong = {
            name for name, weight in SIGNAL_WEIGHTS.items() if weight >= threshold
        }
        weak = {name for name, weight in SIGNAL_WEIGHTS.items() if weight < threshold}

        assert strong == {
            TopicSignal.REF,
            TopicSignal.CONVERSATION,
            TopicSignal.THREAD,
            TopicSignal.SUBJECT,
        }
        assert weak == {TopicSignal.ATTACHMENT, TopicSignal.PARTICIPANTS}
        assert sum(SIGNAL_WEIGHTS[one] for one in weak) >= threshold


class TestDeterministicIds:
    """The property the whole phase's definition of done rests on."""

    def test_a_topic_id_is_the_digest_of_its_members(self) -> None:
        found = topic_id(["b", "a", "c"])

        assert found.startswith("topic:")
        assert len(found) == len("topic:") + 32

    def test_a_topic_id_ignores_order_and_repetition(self) -> None:
        """Union-find picks a root by rank, so member order is an accident.

        A key that depended on it would rename a topic on a rebuild that found
        exactly the same messages — a data change that is not one.
        """
        assert topic_id(["a", "b", "c"]) == topic_id(["c", "a", "b", "a"])

    def test_different_members_give_a_different_topic(self) -> None:
        assert topic_id(["a", "b"]) != topic_id(["a", "b", "c"])

    def test_a_template_id_renders_the_fingerprint_unsigned(self) -> None:
        """The trap, in the one place it would produce a *key* with a minus.

        The graph stores ``to_signed_64(simhash)``, and formatting that value
        emits ``-50a6dea0bb93f5ad``. Two runs disagreeing about a minus sign
        are not a key, so the conversion happens inside the function and a
        caller that already converted loses nothing.
        """
        stored = to_signed_64(TOP_BIT)

        assert stored < 0
        assert template_id(stored, TemplateDirection.SENT) == template_id(
            TOP_BIT, TemplateDirection.SENT
        )
        assert "-" not in template_id(stored, TemplateDirection.SENT)

    def test_a_template_id_is_fixed_width_and_carries_the_direction(self) -> None:
        """Sixteen hex characters, so a leading zero cannot shorten a key,
        and the direction in it, so the two passes cannot collide."""
        sent = template_id(1, TemplateDirection.SENT)
        received = template_id(1, TemplateDirection.RECEIVED)

        assert sent == "template:0000000000000001:sent"
        assert received == "template:0000000000000001:received"
        assert sent != received

    def test_the_conversion_back_and_forth_is_the_same_bits(self) -> None:
        """The premise the rest of the sign handling is built on."""
        assert to_unsigned_64(to_signed_64(TOP_BIT)) == TOP_BIT


class TestTheOgmDeclarations:
    """What runic actually registered — read off the class, not the source."""

    @pytest.mark.parametrize(
        ("node", "label"),
        [
            (Group, "Group"),
            (Topic, "Topic"),
            (Template, "Template"),
            (Community, "Community"),
        ],
    )
    def test_each_derived_node_has_its_own_label_and_an_indexed_key(
        self, node: Any, label: str
    ) -> None:
        """Its own label, because a derived node never becomes ground truth —
        and indexed, because every write is a ``MERGE`` on that key and an
        unindexed one is a label scan per row."""
        key = _field(node, "id")

        assert node._labels == [label]
        assert node._primary_label == label
        assert key.primary_key is True
        assert key.index is True

    @pytest.mark.parametrize(
        ("edge", "name"),
        [
            (CoAddressed, "CO_ADDRESSED"),
            (AddressedGroup, "ADDRESSED_GROUP"),
            (About, "ABOUT"),
            (InstanceOf, "INSTANCE_OF"),
            (MemberOf, "MEMBER_OF"),
            (InCircle, "IN_CIRCLE"),
            (Suggested, "SUGGESTED"),
        ],
    )
    def test_the_seven_derived_edge_types_are_named_as_the_spec_names_them(
        self, edge: Any, name: str
    ) -> None:
        assert edge._edge_type == name

    @pytest.mark.parametrize(
        ("node", "relationship", "edge"),
        [
            (Group, "ADDRESSED_GROUP", AddressedGroup),
            (Topic, "ABOUT", About),
            (Template, "INSTANCE_OF", InstanceOf),
            (Community, "IN_CIRCLE", InCircle),
        ],
    )
    def test_each_relation_points_back_at_message_from_the_derived_side(
        self, node: Any, relationship: str, edge: type
    ) -> None:
        """``INCOMING`` from the derived node, which emits
        ``(message)-[:TYPE]->(derived)`` — the direction §5.2 draws.

        Declared here rather than on ``Message`` on purpose: adding a relation
        to a ground-truth node so that it can describe something derived is
        exactly the mixing this package exists to avoid.
        """
        relation = _field(node, "messages")

        assert relation.relationship == relationship
        assert relation.direction == "INCOMING"
        assert relation.target is Message
        assert relation.edge_model is edge

    @pytest.mark.parametrize("node", [Group, Topic, Template, Community])
    def test_the_timestamps_kept_their_converter(self, node: Any) -> None:
        """The failure the class order in ``model.py`` exists to prevent.

        runic resolves a node's annotations when the class is declared. Name a
        type that does not exist yet and the resolution fails quietly, taking
        the converters off *every* field on that node — after which a
        ``datetime`` reaches the driver as a Python object and a rebuild dies
        somewhere that has nothing to do with the cause.
        """
        for name in ("first_seen", "last_seen"):
            assert _field(node, name).converter is not None

    def test_the_template_direction_kept_its_enum_converter(self) -> None:
        assert _field(Template, "direction").converter is not None

    def test_the_membership_edge_carries_no_properties(self) -> None:
        """Membership is not a judgement, so there is nothing to score.

        A message's ``participant_key`` either is the group's key or it is not;
        a ``method`` on this edge would be a confidence about an equality.
        """
        assert _fields(AddressedGroup) == []

    def test_the_nodes_take_keyword_arguments_and_nothing_else(self) -> None:
        """runic constructors are keyword-only, and a test that constructs one
        positionally is the fastest way to find out."""
        when = datetime(2026, 1, 1, tzinfo=UTC)
        group = Group(
            id="key", size=3, message_count=5, first_seen=when, last_seen=when
        )

        assert (group.id, group.size, group.message_count) == ("key", 3, 5)
        with pytest.raises(TypeError):
            Group("key")  # ty: ignore[missing-argument, too-many-positional-arguments]


class TestTheRebuildStages:
    """Ten stages, and the order is the dependency graph written down."""

    def test_the_stages_are_the_ten_the_spec_names_in_its_order(self) -> None:
        """A rebuild reports one progress row per stage, so the order is what a
        user watches — and it is also what decides whether a stage has what it
        needs. Written out rather than derived, so a reordering shows up here
        before it shows up as a stage reading a property nothing wrote yet.
        """
        assert [stage.value for stage in RebuildStage] == [
            "delete",
            "read",
            "correspondents",
            "centrality",
            "communities",
            "topics",
            "keywords",
            "templates",
            "importance",
            "suggestions",
        ]

    def test_every_stage_runs_after_what_it_reads(self) -> None:
        """The four orderings that are not arbitrary.

        ``CENTRALITY`` before ``COMMUNITIES`` because a community's label and
        every ``MEMBER_OF.rank`` are ranks; ``KEYWORDS`` after ``TOPICS``
        because it needs the clusters; ``IMPORTANCE`` after ``TEMPLATES``
        (the automation score pulls a message down) and after ``CENTRALITY``
        (the sender's rank pushes it up); ``SUGGESTIONS`` last, because it
        needs topics, communities and the current tag memberships at once.
        """
        order = list(RebuildStage)

        def before(one: RebuildStage, other: RebuildStage) -> bool:
            return order.index(one) < order.index(other)

        assert before(RebuildStage.CENTRALITY, RebuildStage.COMMUNITIES)
        assert before(RebuildStage.TOPICS, RebuildStage.KEYWORDS)
        assert before(RebuildStage.TEMPLATES, RebuildStage.IMPORTANCE)
        assert before(RebuildStage.CENTRALITY, RebuildStage.IMPORTANCE)
        assert order[0] is RebuildStage.DELETE
        assert order[-1] is RebuildStage.SUGGESTIONS


class TestTheCounts:
    """What one rebuild did — every stage answers with a number of its own."""

    def test_every_new_stage_reports_a_count_of_its_own(self) -> None:
        """A stage that produced nothing and a stage that never ran look the
        same in a log line unless each has its own field."""
        counts = DerivedCounts()

        assert {
            "communities": counts.communities,
            "circles": counts.circles,
            "ranked_addresses": counts.ranked_addresses,
            "ranked_messages": counts.ranked_messages,
            "keyworded_topics": counts.keyworded_topics,
            "scored_messages": counts.scored_messages,
            "suggestions": counts.suggestions,
            "algorithms_skipped": counts.algorithms_skipped,
        } == dict.fromkeys(
            (
                "communities",
                "circles",
                "ranked_addresses",
                "ranked_messages",
                "keyworded_topics",
                "scored_messages",
                "suggestions",
                "algorithms_skipped",
            ),
            0,
        )

    def test_a_skipped_algorithm_is_a_number_and_not_an_absence(self) -> None:
        """The guard §5.1 asks for: a procedure that refuses an empty graph
        leaves its stage at zero, and a rebuild that reported nothing would
        otherwise be indistinguishable from one that found nothing."""
        counts = DerivedCounts(algorithms_skipped=2, communities=0)

        assert (counts.algorithms_skipped, counts.communities) == (2, 0)


class TestCommunityIds:
    """A community is the set of addresses in it, and its key says so."""

    def test_a_community_id_is_the_digest_of_its_members(self) -> None:
        """Keyed on membership for the reason a topic is: FalkorDB's label
        propagation has no seed, so a run that produced the same partition must
        produce the same node — and a run that produced a different partition
        must not silently update the old one."""
        found = community_id([corpus.ANNA, corpus.THOMAS])

        assert found.startswith("community:")
        assert len(found) == len("community:") + 32

    def test_a_community_id_ignores_order_and_repetition(self) -> None:
        assert community_id([corpus.THOMAS, corpus.ANNA, corpus.ANNA]) == community_id(
            [corpus.ANNA, corpus.THOMAS]
        )

    def test_different_members_give_a_different_community(self) -> None:
        assert community_id([corpus.ANNA]) != community_id([corpus.THOMAS])


class TestTheNewFindings:
    """The value objects the four new analyses pass between their halves."""

    def test_a_community_counts_its_own_members_and_messages(self) -> None:
        """Both counts are derived from the mappings they describe, so the node
        this becomes cannot claim a size its ``MEMBER_OF`` edges do not have."""
        facts = CommunityFacts(
            id=community_id([corpus.ANNA, corpus.THOMAS]),
            label="kunde.example",
            members={corpus.ANNA: 0.9, corpus.THOMAS: 0.4},
            messages={corpus.canonical("p1"): 1.0},
        )

        assert (facts.size, facts.message_count) == (2, 1)
        assert facts.method == "lpa"

    def test_a_finding_carries_what_the_guard_stepped_over(self) -> None:
        """§5.1's skipped procedure calls reach ``DerivedCounts`` from here."""
        findings = CommunityFindings(skipped=1)

        assert (findings.communities, findings.skipped) == ((), 1)

    def test_a_score_keeps_its_reasons_and_the_version_that_made_it(self) -> None:
        """A number without the vocabulary behind it is not explainable, and a
        number without a version is not comparable with the next run's."""
        score = ImportanceScore(
            message_id=corpus.canonical("p1"),
            score=0.75,
            reasons=("addressed directly", "replied by you"),
            version="1",
        )

        assert score.reasons == ("addressed directly", "replied by you")
        assert score.version == "1"

    def test_a_suggestion_names_the_group_that_made_it(self) -> None:
        """``method`` is the vocabulary a user reads before accepting: a thread
        is a stronger reason than a community."""
        suggestion = Suggestion(
            tag_id="tag:nord-42",
            message_id=corpus.canonical("p2"),
            score=0.6,
            method=GroupingKind.THREAD,
        )
        grouping = Grouping(
            kind=GroupingKind.COMMUNITY,
            members=(corpus.canonical("p1"), corpus.canonical("p2")),
        )

        assert suggestion.method == "thread"
        assert [kind.value for kind in GroupingKind] == [
            "thread",
            "topic",
            "community",
        ]
        assert grouping.members == (corpus.canonical("p1"), corpus.canonical("p2"))

    def test_signals_default_to_the_absence_of_every_signal(self) -> None:
        """A message nothing was read for scores on its own properties alone,
        rather than failing the scorer with a missing attribute."""
        signals = MessageSignals(id=corpus.canonical("w1"))

        assert (signals.reply_count, signals.has_attachments) == (0, False)
        assert (signals.sent_to, signals.replied_by, signals.label_names) == (
            (),
            (),
            (),
        )


class TestTheAnnotationLayerStaysOutOfReach:
    """``Tag`` is core's, and the derived model may only point at it."""

    def test_the_suggestion_edge_carries_a_score_and_a_method(self) -> None:
        """A suggestion is a judgement, so it says how strong and why —
        exactly what ``TAGGED`` deliberately does not carry."""
        assert {one.name for one in _fields(Suggested)} == {"score", "method"}

    def test_the_circle_edge_says_how_much_of_the_message_is_in_it(self) -> None:
        assert {one.name for one in _fields(InCircle)} == {"score", "method"}

    def test_the_membership_edge_carries_the_rank(self) -> None:
        assert {one.name for one in _fields(MemberOf)} == {"rank"}

    def test_a_topic_keeps_its_keywords(self) -> None:
        """Written by ``MERGE_TOPIC_KEYWORDS`` and by nothing else."""
        assert _field(Topic, "keywords") is not None

    def test_a_community_points_at_its_addresses(self) -> None:
        """``INCOMING`` from the community, which emits
        ``(address)-[:MEMBER_OF]->(community)`` — and declared here rather than
        on ``Address``, so ground truth never describes a derived thing."""
        relation = _field(Community, "members")

        assert relation.relationship == "MEMBER_OF"
        assert relation.direction == "INCOMING"
        assert relation.edge_model is MemberOf
