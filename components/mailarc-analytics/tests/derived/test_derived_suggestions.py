"""Which messages a tag might want next, and the two guards that keep it quiet.

A tag is a human's word for a set of messages, so a suggestion is an argument
and never a decision: it becomes a ``SUGGESTED`` edge the next rebuild deletes,
and only ``TAGGED`` — written by ``mailarc-core``, by a person — records what
was decided. What this pass has to get right is being *quiet enough to be
worth reading*: one tagged message on a busy thread must not produce fifty
suggestions, and two tagged messages out of two hundred must not produce a
hundred and ninety-eight.

Both guards are below, together with the rule that a suggestion is scored by
the best group arguing for it rather than by every group put together — so two
weak arguments cannot outvote one strong one.
"""

from itertools import pairwise

import corpus
import pytest

from mailarc_analytics import (
    CommunityFacts,
    Grouping,
    GroupingKind,
    SimilarityEdge,
    TopicCluster,
    suggest,
)
from mailarc_analytics.derived.conversations import CONVERSATION_METHOD
from mailarc_analytics.derived.model import TopicMember
from mailarc_analytics.derived.suggestions import SUGGESTION_WEIGHTS, groupings

CONFIG = corpus.calibrated_config()

TAG = "tag:nord-42"


def _group(kind: GroupingKind, *members: str) -> Grouping:
    return Grouping(kind=kind, members=tuple(sorted(members)))


def _thread(*members: str) -> Grouping:
    return _group(GroupingKind.THREAD, *members)


def _chain(*members: str) -> tuple[SimilarityEdge, ...]:
    """What :func:`conversation_edges` hands over: ``n-1`` edges, not a set."""
    return tuple(
        SimilarityEdge(left=left, right=right, method=CONVERSATION_METHOD, weight=0.9)
        for left, right in pairwise(members)
    )


def _cluster(key: str, *members: str) -> TopicCluster:
    return TopicCluster(
        id=key,
        members=tuple(
            TopicMember(message_id=one, score=0.9, method="ref") for one in members
        ),
    )


def _circle(*messages: str) -> CommunityFacts:
    return CommunityFacts(
        id="community:x", messages=dict.fromkeys(messages, 1.0), members={"a@b": 0.1}
    )


def test_a_group_with_enough_tagged_members_offers_the_rest() -> None:
    """The finding. Two of four wear the tag, so the other two are offered."""
    found = suggest(
        {TAG: frozenset({"m1", "m2"})}, (_thread("m1", "m2", "m3", "m4"),), CONFIG
    )

    assert [(one.tag_id, one.message_id) for one in found] == [
        (TAG, "m3"),
        (TAG, "m4"),
    ]


def test_one_tagged_member_is_a_coincidence_and_not_an_argument() -> None:
    """Somebody tagged one message that happens to share a thread with fifty.

    Without this guard the first tag on a busy thread produces fifty
    suggestions, which is how a suggestion list stops being read.
    """
    members = [f"m{index:02d}" for index in range(50)]

    found = suggest({TAG: frozenset({"m00"})}, (_thread(*members),), CONFIG)

    assert found == ()
    assert CONFIG.tag_suggest_min_tagged == 2


def test_a_handful_tagged_out_of_a_mailing_list_is_not_an_argument_either() -> None:
    """The other half of the same guard, and the half that scales.

    Two out of five is a project; two out of two hundred is a mailing list
    somebody filed twice.
    """
    members = [f"m{index:03d}" for index in range(200)]

    found = suggest({TAG: frozenset({"m000", "m001"})}, (_thread(*members),), CONFIG)

    assert found == ()
    assert CONFIG.tag_suggest_min_share == 0.3


def test_a_message_that_already_wears_the_tag_is_never_suggested() -> None:
    """``TAGGED`` is the decision; suggesting it again would offer to redo it.

    It would also overwrite nothing and cost the user a row to dismiss, on
    every rebuild, forever.
    """
    found = suggest(
        {TAG: frozenset({"m1", "m2"})}, (_thread("m1", "m2", "m3"),), CONFIG
    )

    assert [one.message_id for one in found] == ["m3"]


def test_the_strongest_group_arguing_for_a_message_sets_its_score() -> None:
    """The maximum over the groups, not the sum.

    Two weak arguments must not outvote one strong one: a message in a
    community with a couple of tagged members and in a thread that is nearly
    all tagged is a thread suggestion, and the number a user reads has to say
    which.
    """
    tagged = {TAG: frozenset({"m1", "m2"})}
    groups = (
        _group(GroupingKind.COMMUNITY, "m1", "m2", "m9"),
        _thread("m1", "m2", "m9"),
    )

    found = suggest(tagged, groups, CONFIG)

    assert [one.method for one in found] == [GroupingKind.THREAD]
    assert found[0].score == pytest.approx(
        SUGGESTION_WEIGHTS[GroupingKind.THREAD] * 2 / 3
    )


def test_a_circle_alone_never_reaches_the_auto_accept_threshold() -> None:
    """What ``tag_auto_accept_min_score`` was chosen above.

    "These people write to each other" is the weakest of the three arguments,
    and auto-accept writes to the annotation layer — the one place in this
    application that is supposed to hold only what a person decided. So however
    many of a circle's messages wear the tag, a circle on its own may never
    tag another one; a thread or a topic can.
    """
    every_member = {TAG: frozenset({"m1", "m2", "m3"})}
    groups = (_group(GroupingKind.COMMUNITY, "m1", "m2", "m3", "m4"),)

    found = suggest(every_member, groups, CONFIG)

    assert found[0].score < CONFIG.tag_auto_accept_min_score
    assert SUGGESTION_WEIGHTS[GroupingKind.COMMUNITY] < CONFIG.tag_auto_accept_min_score


def test_a_thread_that_is_nearly_all_tagged_does_reach_it() -> None:
    """The other side of the same threshold, so the setting is not dead."""
    found = suggest(
        {TAG: frozenset({"m1", "m2", "m3"})}, (_thread("m1", "m2", "m3", "m4"),), CONFIG
    )

    assert found[0].score >= CONFIG.tag_auto_accept_min_score


def test_an_archive_with_no_tags_produces_no_suggestions() -> None:
    """The normal state before anybody has promoted a cluster, and not a
    failure of the pass."""
    assert suggest({}, (_thread("m1", "m2", "m3"),), CONFIG) == ()


def test_every_tag_is_answered_separately() -> None:
    """Two tags over the same thread are two arguments, not one."""
    tagged = {
        TAG: frozenset({"m1", "m2"}),
        "tag:other": frozenset({"m1", "m3"}),
    }

    found = suggest(tagged, (_thread("m1", "m2", "m3", "m4"),), CONFIG)

    assert {(one.tag_id, one.message_id) for one in found} == {
        (TAG, "m3"),
        (TAG, "m4"),
        ("tag:other", "m2"),
        ("tag:other", "m4"),
    }


def test_the_suggestions_come_back_in_a_stable_order() -> None:
    """Sorted by tag and then by message, so two rebuilds write the same rows."""
    tagged = {"tag:b": frozenset({"m1", "m2"}), "tag:a": frozenset({"m1", "m2"})}

    found = suggest(tagged, (_thread("m1", "m2", "m3", "m4"),), CONFIG)

    assert [(one.tag_id, one.message_id) for one in found] == sorted(
        (one.tag_id, one.message_id) for one in found
    )


def test_a_group_whose_members_are_all_tagged_offers_nothing() -> None:
    """Nothing left to argue for, and no empty row to write."""
    assert suggest({TAG: frozenset({"m1", "m2"})}, (_thread("m1", "m2"),), CONFIG) == ()


def test_an_empty_group_is_not_a_division_by_zero() -> None:
    """A topic whose members were purged with an account is a real state."""
    assert suggest({TAG: frozenset({"m1"})}, (_thread(),), CONFIG) == ()


def test_the_three_weights_are_ordered_the_way_the_arguments_are() -> None:
    """A thread is the mail itself saying these messages answer each other; a
    topic is a cluster; a community is only "these people talk"."""
    assert (
        SUGGESTION_WEIGHTS[GroupingKind.THREAD]
        > SUGGESTION_WEIGHTS[GroupingKind.TOPIC]
        > SUGGESTION_WEIGHTS[GroupingKind.COMMUNITY]
    )


class TestWhereTheGroupsComeFrom:
    """The three earlier stages, turned into the one shape :func:`suggest` reads.

    ``suggest`` deliberately knows nothing about threads, topics or circles —
    it takes "these ids, for this reason" — so something has to do the turning,
    and it is this module rather than the rebuild: an orchestrator that reached
    into a ``TopicCluster`` for its member ids and into a ``CommunityFacts``
    for its messages would be the only place in the package where the shape of
    two other analyses' findings is known.
    """

    def test_a_conversations_chain_edges_become_one_thread_group(self) -> None:
        """§5.2's conversations arrive as ``n-1`` chain edges per exchange, not
        as components — this is where the component comes back."""
        found = groupings(_chain("m1", "m2", "m3"), (), ())

        assert found == (
            Grouping(kind=GroupingKind.THREAD, members=("m1", "m2", "m3")),
        )

    def test_two_exchanges_are_two_groups_and_not_one(self) -> None:
        """The union-find is over the edges, so two chains that share nobody
        stay apart — which is the difference between suggesting a project's
        mail and suggesting the archive."""
        found = groupings(_chain("m1", "m2") + _chain("m8", "m9"), (), ())

        assert [one.members for one in found] == [("m1", "m2"), ("m8", "m9")]

    def test_a_topic_becomes_a_topic_group_of_its_members(self) -> None:
        found = groupings((), (_cluster("topic:x", "m3", "m1"),), ())

        assert found == (Grouping(kind=GroupingKind.TOPIC, members=("m1", "m3")),)

    def test_a_circle_becomes_a_community_group_of_the_mail_in_it(self) -> None:
        """The messages placed in the circle, not its addresses: a suggestion
        is always about mail, and a circle's members are people."""
        found = groupings((), (), (_circle("m2", "m1"),))

        assert found == (Grouping(kind=GroupingKind.COMMUNITY, members=("m1", "m2")),)

    def test_the_kinds_come_out_strongest_first(self) -> None:
        """Not required by :func:`suggest`, which takes the best of every group
        — but a stable order is what makes two rebuilds write the same rows and
        a log line readable."""
        found = groupings(
            _chain("m1", "m2"),
            (_cluster("topic:x", "m1", "m3"),),
            (_circle("m1", "m4"),),
        )

        assert [one.kind for one in found] == [
            GroupingKind.THREAD,
            GroupingKind.TOPIC,
            GroupingKind.COMMUNITY,
        ]

    def test_nothing_at_all_is_no_groups_rather_than_a_failure(self) -> None:
        """A first rebuild of an archive nobody has tagged."""
        assert groupings((), (), ()) == ()

    def test_a_lone_message_is_not_a_group(self) -> None:
        """A topic of one cannot argue for anything and a chain of one has no
        edge, so neither reaches :func:`suggest` to be divided by."""
        found = groupings((), (_cluster("topic:x", "m1"),), ())

        assert found == ()
