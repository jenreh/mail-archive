"""Signal 7 — one exchange, however many mailboxes it was imported from.

A2 already knows about threads, and that is exactly why this exists. A
``Thread`` node groups the copies **one** account holds: the provider hands
over its own thread id, so the same conversation archived from two mailboxes
arrives as two threads and the thread signal joins neither to the other.
``In-Reply-To`` is the other half — the sender's own statement that this
message answers that one — and it crosses accounts, because it is a header the
mail carries rather than a filing decision a provider made.

Union-find over both, and nothing else. There is no ``algo.WCC`` here although
FalkorDB has one: the answer is connected components over a relation this
package already holds in memory, which :class:`DisjointSet` gives in near-linear
time, deterministically, on a graph that may not have a ``REPLIES_TO`` edge yet.
Asking the store would cost a round trip, a guard and a procedure that throws on
exactly the archive a first rebuild is looking at.

What comes out is :class:`~mailarc_analytics.derived.model.SimilarityEdge`
values, so the seam is the one A2 already has: ``build_topics`` applies
``extra_edges`` after the exact signals have settled, which is what keeps a
conversation from overriding a ticket token. **n-1 chain edges per component**
and not every pair — a conversation of a hundred messages is the same component
either way, and the complete graph would be five thousand rows handed to a
union-find that needs ninety-nine of them.

Pure. Takes the facts and a reply mapping, returns edges, touches no session.
"""

import logging
from collections.abc import Mapping, Sequence
from itertools import pairwise

from mailarc_analytics.derived.model import (
    SIGNAL_WEIGHTS,
    MessageFacts,
    SimilarityEdge,
    TopicSignal,
)
from mailarc_analytics.derived.partition import DisjointSet

logger = logging.getLogger(__name__)

CONVERSATION_METHOD = TopicSignal.CONVERSATION.value
"""What an ``ABOUT`` edge joined this way says about itself."""


def conversation_edges(
    facts: Sequence[MessageFacts], replies: Mapping[str, str]
) -> tuple[SimilarityEdge, ...]:
    """Every exchange in *facts*, as chain edges A2 can apply. No I/O.

    *replies* is ``{reply id: parent id}``, which is what
    :func:`~mailarc_analytics.derived.reader.read_replies` hands back.

    **Both ends are joined against the ids actually in hand.** The reply read
    is paged over the *reply table* while the facts read is paged over the
    archive, so those are two different prefixes and a capped rebuild can
    legitimately hold a reply whose parent it never read. A
    :class:`~mailarc_analytics.derived.partition.DisjointSet` is built over
    exactly the message set it was given and raises on an id it was not, which
    is the right behaviour for a caller that lost track of its own messages and
    the wrong one for a ceiling doing its job — so the unknown end is dropped
    here, where the difference is known.

    An empty or missing ``thread_id`` joins nothing. ``None`` is what the
    reader returns for a message no provider threaded, and ``""`` is what a
    provider that threads everything into one bucket would look like; folding
    either into a bucket of its own would make the first rebuild over an IMAP
    archive a single conversation.
    """
    thread_of = {one.id: one.thread_id or "" for one in facts}
    partition = DisjointSet(sorted(thread_of))
    threads: dict[str, list[str]] = {}
    for message, thread in thread_of.items():
        if thread:
            threads.setdefault(thread, []).append(message)
    for members in threads.values():
        for left, right in pairwise(sorted(members)):
            partition.union(left, right)

    crossed = 0
    for reply, parent in sorted(replies.items()):
        if reply not in thread_of or parent not in thread_of or reply == parent:
            continue
        here, there = thread_of[reply], thread_of[parent]
        if here and there and here != there:
            crossed += 1
        partition.union(reply, parent)

    found = tuple(
        SimilarityEdge(
            left=left,
            right=right,
            method=CONVERSATION_METHOD,
            weight=SIGNAL_WEIGHTS[TopicSignal.CONVERSATION],
        )
        for members in partition.components()
        if len(members) > 1
        for left, right in pairwise(members)
    )
    logger.info(
        "Signal 7 found %d conversation edges over %d messages (%d reply links "
        "join two different threads)",
        len(found),
        len(thread_of),
        crossed,
    )
    return found
