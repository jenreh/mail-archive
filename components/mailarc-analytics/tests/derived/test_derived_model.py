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
    CorrespondentFindings,
    DerivedCounts,
    Group,
    GroupFacts,
    InstanceOf,
    MessageFacts,
    RebuildProgress,
    SimilarityEdge,
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
    template_id,
    topic_id,
)
from mailarc_core.archive.model import Message, to_signed_64, to_unsigned_64

VALUE_OBJECTS = (
    MessageFacts, CoAddressedPair, GroupFacts, CorrespondentFindings,
    SimilarityEdge, TopicMember, TopicCluster, TopicFindings,
    TemplateMember, TemplateGroup, TemplateGrouping, TemplateCluster,
    RebuildProgress, DerivedCounts,
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
            TopicSignal.THREAD: 0.8,
            TopicSignal.SUBJECT: 0.5,
            TopicSignal.ATTACHMENT: 0.4,
            TopicSignal.PARTICIPANTS: 0.2,
        }

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

        assert strong == {TopicSignal.REF, TopicSignal.THREAD, TopicSignal.SUBJECT}
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
        ("node", "label"), [(Group, "Group"), (Topic, "Topic"), (Template, "Template")]
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
        ],
    )
    def test_the_four_derived_edge_types_are_named_as_the_spec_names_them(
        self, edge: Any, name: str
    ) -> None:
        assert edge._edge_type == name

    @pytest.mark.parametrize(
        ("node", "relationship", "edge"),
        [
            (Group, "ADDRESSED_GROUP", AddressedGroup),
            (Topic, "ABOUT", About),
            (Template, "INSTANCE_OF", InstanceOf),
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

    @pytest.mark.parametrize("node", [Group, Topic, Template])
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
