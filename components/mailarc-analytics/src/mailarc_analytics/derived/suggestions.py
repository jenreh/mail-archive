"""Which messages a tag might want next — an argument, never a decision.

The annotation layer is the one part of this graph that holds what a *person*
decided, and it survives every rebuild for exactly that reason. Nothing in
``mailarc-analytics`` may write to it: this pass produces ``SUGGESTED`` edges,
which are derived and disposable, and a user turns one into a ``TAGGED``
membership through :mod:`mailarc_core.archive.tags` — or, with
``tag_auto_accept`` on, ``app.derive`` does it for them and marks the membership
``TagSource.AUTO`` so what the analysis did stays visible.

One function answers for all three kinds of group, because a thread, a topic and
a community are all just "these ids, for this reason". That is also why this
module needs no graph: the groups arrive as
:class:`~mailarc_analytics.derived.findings.Grouping` values and the current
memberships as a mapping.

**The hard part is being quiet.** Two guards, and both are shares of the same
thing seen from opposite ends: a group needs at least
``tag_suggest_min_tagged`` members already wearing the tag — one is a
coincidence, and would turn the first tag on a busy thread into fifty
suggestions — and at least ``tag_suggest_min_share`` of it must, because two
tagged messages out of two hundred is a mailing list somebody filed twice.

A suggestion is scored by the **best** group arguing for it and not by all of
them together, so two weak arguments cannot outvote one strong one, and the
method it carries is the one that won.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType

from runic.ogm import Session

from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.findings import (
    CommunityFacts,
    Grouping,
    GroupingKind,
    Suggestion,
)
from mailarc_analytics.derived.model import SimilarityEdge, Suggested, TopicCluster
from mailarc_analytics.derived.partition import DisjointSet
from mailarc_analytics.derived.writes import merge_rows
from mailarc_analytics.queries import catalog

logger = logging.getLogger(__name__)

SUGGESTION_WEIGHTS: Mapping[GroupingKind, float] = MappingProxyType(
    {
        GroupingKind.THREAD: 0.9,
        GroupingKind.TOPIC: 0.7,
        GroupingKind.COMMUNITY: 0.5,
    }
)
"""What each kind of group's argument is worth, strongest first.

Ordered the way the evidence is: a thread is the mail itself saying these
messages answer each other, a topic is one of A2's clusters — a shared ticket
token or a shared attachment — and a community is only "these people write to
each other", which is true of everybody in a department.

The community weight is deliberately **below**
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.tag_auto_accept_min_score`.
``score`` is ``weight × k/n`` and ``k/n`` cannot exceed one, so however much of
a circle already wears a tag, a circle on its own can never auto-accept another
message onto it. A thread or a topic can. Auto-accept writes to the annotation
layer, and that is the one place a weak argument must not reach.

A calibration rather than configuration, for
:data:`~mailarc_analytics.derived.model.SIGNAL_WEIGHTS`' reason: the three
numbers only mean anything relative to each other and to that threshold.
"""


def groupings(
    conversations: Sequence[SimilarityEdge],
    clusters: Sequence[TopicCluster],
    circles: Sequence[CommunityFacts],
) -> tuple[Grouping, ...]:
    """The three earlier stages as the one shape :func:`suggest` reads. No I/O.

    :func:`suggest` takes "these ids, for this reason" and knows nothing about
    threads, topics or circles, which is what lets every rule about a
    suggestion be tested without any of them. Somebody still has to do the
    turning, and it is this module rather than the rebuild: an orchestrator
    reaching into a :class:`~mailarc_analytics.derived.model.TopicCluster` for
    its member ids and into a
    :class:`~mailarc_analytics.derived.findings.CommunityFacts` for its
    messages would be the one place in the package where two other analyses'
    findings are unpacked by something that is not them.

    **Conversations arrive as edges and leave as components.**
    :func:`~mailarc_analytics.derived.conversations.conversation_edges` emits
    ``n-1`` chain edges per exchange, because that is what A2's union-find
    needs; a group is the exchange itself, so the components are found again
    here. Over chain edges that is near-linear in the number of messages
    already in one, which is cheaper than carrying a second answer through two
    stages that have no use for it.

    A group of one is dropped at both ends: it can argue for nothing, and it
    would be a share of one over one that
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.tag_suggest_min_tagged`
    has to reject rather than never see.

    Ordered thread, topic, circle — the order
    :data:`SUGGESTION_WEIGHTS` puts them in and the order the evidence is
    strong in — so two rebuilds hand :func:`suggest` the same sequence.
    """
    found = [
        *(_grouping(GroupingKind.THREAD, one) for one in _components(conversations)),
        *(
            _grouping(GroupingKind.TOPIC, (one.message_id for one in cluster.members))
            for cluster in clusters
        ),
        *(_grouping(GroupingKind.COMMUNITY, circle.messages) for circle in circles),
    ]
    answer = tuple(one for one in found if len(one.members) > 1)
    logger.debug(
        "Offered %d groups to argue from, out of %d exchanges, %d topics and "
        "%d circles",
        len(answer),
        len(conversations),
        len(clusters),
        len(circles),
    )
    return answer


def suggest(
    tagged: Mapping[str, frozenset[str]],
    groups: Sequence[Grouping],
    config: AnalyticsConfig,
) -> tuple[Suggestion, ...]:
    """Every message a tag might want, at most one row per tag and message.

    *tagged* is what :func:`~mailarc_analytics.derived.reader.read_tagged` read
    — the memberships as they stand after every other stage, which is why this
    one runs last.

    An archive with no tags at all answers with nothing, and that is the normal
    state before anybody has promoted a cluster rather than a failure of the
    pass.
    """
    found: dict[tuple[str, str], Suggestion] = {}
    for tag, members in sorted(tagged.items()):
        for group in groups:
            _offer(found, tag, members, group, config)
    answer = tuple(
        found[key] for key in sorted(found, key=lambda one: (one[0], one[1]))
    )
    logger.info(
        "Suggested %d memberships over %d tags and %d groups",
        len(answer),
        len(tagged),
        len(groups),
    )
    return answer


def write_suggestions(session: Session, found: Sequence[Suggestion]) -> int:
    """Write the ``SUGGESTED`` edges; return how many rows were sent.

    Both endpoints are matched and neither is merged. The ``Tag`` end is the
    reason that matters: a row naming a tag a human deleted between the read
    and the write has to write nothing, where a merge would resurrect it as an
    empty node with a name nobody chose.
    """
    written = merge_rows(
        session,
        catalog.MERGE_SUGGESTED,
        (
            {
                "message_id": one.message_id,
                "tag_id": one.tag_id,
                "score": one.score,
                "method": str(one.method),
            }
            for one in found
        ),
        model=Suggested,
    )
    logger.info("Wrote %d suggestions", written)
    return written


def _offer(
    found: dict[tuple[str, str], Suggestion],
    tag: str,
    members: frozenset[str],
    group: Grouping,
    config: AnalyticsConfig,
) -> None:
    """Let one group argue for one tag, keeping only the strongest case.

    An equal score between two *different* kinds goes to the stronger kind —
    a thread of six half tagged and a community of three fully tagged can
    arrive at the same number, and the thread is the better argument. Two
    groups of the same kind at the same score carry the same ``method``, so
    there is nothing left to break and the standing row stays: whichever won,
    the edge is identical and two rebuilds write the same one.
    """
    people = frozenset(group.members)
    if not people:
        return
    wearing = people & members
    if len(wearing) < config.tag_suggest_min_tagged:
        return
    share = len(wearing) / len(people)
    if share < config.tag_suggest_min_share:
        return
    weight = SUGGESTION_WEIGHTS[group.kind]
    made = (round(weight * share, 6), weight)
    for message in sorted(people - members):
        key = (tag, message)
        standing = found.get(key)
        if standing is None or made > _strength(standing):
            found[key] = Suggestion(
                tag_id=tag, message_id=message, score=made[0], method=group.kind
            )


def _strength(standing: Suggestion) -> tuple[float, float]:
    """How good the case already on the books is — score, then kind."""
    kind = (
        standing.method
        if isinstance(standing.method, GroupingKind)
        else GroupingKind(standing.method)
    )
    return (standing.score, SUGGESTION_WEIGHTS[kind])


def _grouping(kind: GroupingKind, members: Iterable[str]) -> Grouping:
    """One group with its members sorted and deduplicated, as the docstring of
    :class:`~mailarc_analytics.derived.findings.Grouping` requires."""
    return Grouping(kind=kind, members=tuple(sorted(set(members))))


def _components(conversations: Sequence[SimilarityEdge]) -> list[list[str]]:
    """The exchanges behind a set of chain edges.

    A :class:`~mailarc_analytics.derived.partition.DisjointSet` raises on an id
    it was not given, so it is built over exactly the ids the edges name — the
    edges are the only statement about membership there is here, and a message
    in no edge is in no exchange.
    """
    ends = {one.left for one in conversations} | {one.right for one in conversations}
    if not ends:
        return []
    partition = DisjointSet(sorted(ends))
    for edge in conversations:
        partition.union(edge.left, edge.right)
    return partition.components()
