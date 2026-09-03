"""Signal 7 — the exchange a provider's thread id cannot see whole.

A ``Thread`` groups the copies **one** account holds, so the same conversation
imported from two mailboxes is two threads and A2's thread signal joins neither
to the other. ``In-Reply-To`` is the sender's own statement that this message
answers that one, and it crosses accounts because it is a header rather than a
provider's filing. Union-find over both is what makes one conversation one
component, and that is the whole claim of this file.

Hand-built facts rather than the planted corpus. The corpus has one real
conversation and it is inside a single account, which is precisely the case
that cannot distinguish a union over both signals from a union over the thread
alone. Two accounts have to be planted for the failing case to exist at all.
"""

import corpus
import pytest

from mailarc_analytics import (
    SIGNAL_WEIGHTS,
    MessageFacts,
    SimilarityEdge,
    TopicSignal,
    build_topics,
    conversation_edges,
)

CONFIG = corpus.calibrated_config()

CONVERSATION_WEIGHT = SIGNAL_WEIGHTS[TopicSignal.CONVERSATION]


def _fact(key: str, *, thread: str | None = None) -> MessageFacts:
    """One message reduced to what this analysis reads: an id and a thread."""
    return MessageFacts(id=key, thread_id=thread)


def _pairs(edges: tuple[SimilarityEdge, ...]) -> list[tuple[str, str]]:
    return [(edge.left, edge.right) for edge in edges]


def test_a_thread_and_a_reply_join_one_conversation_across_two_accounts() -> None:
    """The finding the signal exists for.

    ``a1``/``a2`` are one account's copy of the exchange and ``b1``/``b2`` the
    other's, so the two thread ids never meet. The reply header on ``b1``
    names ``a2``, and that single edge is what makes the four of them one
    conversation. A union over thread ids alone answers two components here,
    and the assertion below is that it does not.
    """
    facts = (
        _fact("a1", thread="t-anna"),
        _fact("a2", thread="t-anna"),
        _fact("b1", thread="t-thomas"),
        _fact("b2", thread="t-thomas"),
    )

    edges = conversation_edges(facts, {"a2": "a1", "b1": "a2", "b2": "b1"})

    joined = {edge.left for edge in edges} | {edge.right for edge in edges}
    assert joined == {"a1", "a2", "b1", "b2"}
    assert len(edges) == 3, "n-1 chain edges for one component of four"


def test_a_message_that_answers_nobody_and_shares_no_thread_stays_alone() -> None:
    """A component of one produces no edge at all.

    An edge from a message to itself would be a topic of one after
    ``build_topics`` dropped every singleton — and worse, it would say a
    conversation exists where the archive holds one letter.
    """
    facts = (_fact("a1", thread="t-anna"), _fact("a2", thread="t-anna"), _fact("solo"))

    edges = conversation_edges(facts, {"a2": "a1"})

    assert _pairs(edges) == [("a1", "a2")]


def test_every_edge_carries_the_conversation_signal_and_its_weight() -> None:
    """``method`` and ``weight`` are what ``ABOUT`` will say about the join.

    The weight is read out of :data:`SIGNAL_WEIGHTS` rather than written here,
    because the number only means anything relative to the other six and a
    literal in a test would let the two drift apart.
    """
    edges = conversation_edges((_fact("a1", thread="t"), _fact("a2", thread="t")), {})

    assert [(edge.method, edge.weight) for edge in edges] == [
        (TopicSignal.CONVERSATION.value, CONVERSATION_WEIGHT)
    ]


def test_a_component_of_four_is_a_chain_and_not_every_pair() -> None:
    """n-1 edges, not n(n-1)/2.

    A conversation of a hundred messages is a component either way, and the
    complete graph would be five thousand pairs handed to a union-find that
    needs ninety-nine of them. The chain is over the members in sorted order,
    so two rebuilds produce the same edges.
    """
    facts = tuple(_fact(key, thread="t") for key in ("d", "a", "c", "b"))

    edges = conversation_edges(facts, {})

    assert _pairs(edges) == [("a", "b"), ("b", "c"), ("c", "d")]


def test_a_parent_nobody_read_joins_nothing() -> None:
    """The reply table is paged over *replies* and the facts over messages.

    Those are two different prefixes of the archive, so a capped rebuild can
    hold a reply whose parent it never read. Joining against the ids actually
    in hand is where that is resolved — the alternative is a ``DisjointSet``
    raising ``KeyError`` on an id it was never given.
    """
    facts = (_fact("a1"), _fact("a2"))

    edges = conversation_edges(facts, {"a1": "gone", "a2": "a1"})

    assert _pairs(edges) == [("a1", "a2")]


def test_an_empty_thread_id_joins_nothing() -> None:
    """A message with no thread is not in the "" thread with every other one.

    The reader hands back ``None`` for a message a provider never threaded,
    and an empty string is what a provider that threads everything into one
    bucket would look like. Both have to fall out, or the first rebuild over an
    IMAP archive is one conversation.
    """
    facts = (_fact("a1"), _fact("a2", thread=""), _fact("a3", thread=None))

    assert conversation_edges(facts, {}) == ()


def test_the_edges_go_through_build_topics_as_the_seventh_signal() -> None:
    """The seam, end to end, without a graph.

    ``build_topics`` applies ``extra_edges`` after the exact signals have
    settled, so a conversation can only join what a fact left open — which is
    exactly what two single-account threads are.
    """
    facts = (
        _fact("a1", thread="t-anna"),
        _fact("a2", thread="t-anna"),
        _fact("b1", thread="t-thomas"),
    )
    edges = conversation_edges(facts, {"b1": "a2"})

    found = build_topics(facts, CONFIG, extra_edges=edges)

    assert len(found.clusters) == 1
    assert [one.message_id for one in found.clusters[0].members] == ["a1", "a2", "b1"]


@pytest.mark.parametrize("replies", [{"a2": "a1"}, {"a1": "a2"}])
def test_the_answer_does_not_depend_on_which_way_the_header_points(
    replies: dict[str, str],
) -> None:
    """A reply edge is evidence of one exchange whichever end it was read from.

    The union is symmetric by construction; this pins that nothing downstream
    started to care, because the chain is built from the sorted component.
    """
    facts = (_fact("a1"), _fact("a2"))

    assert _pairs(conversation_edges(facts, replies)) == [("a1", "a2")]
