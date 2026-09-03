"""Circles of correspondents — what label propagation found, made durable.

``Group`` answers the exact question and this answers the inexact one. A group
is a set of people who were repeatedly on the *same message*, keyed by the hash
the import already computed over the participants; a community is a set of
people who write to each other without anybody ever having addressed them as a
set. The two are deliberately different findings, and a circle is not a big
group.

The partition itself comes from FalkorDB's ``algo.labelPropagation`` over
``CO_ADDRESSED`` and arrives here as a plain mapping, which is what keeps this
module pure and every rule below testable without a store. What this module
does is turn that mapping into something that survives a second rebuild, and
that takes three decisions R1 forced:

* **Keyed by the digest of its members.** LPA takes no seed, so the community
  *number* is the one thing about its answer that may legitimately move between
  two runs over an unchanged graph. A node keyed on it would be renamed every
  time that happened; :func:`~mailarc_analytics.derived.model.community_id`
  cannot be.
* **Labelled by the most common domain**, tie going to the domain of the
  best-ranked member. Counting alone would leave the answer to whichever domain
  the iteration reached first, which is a dict order rather than a fact.
* **One circle per message at most** — the one with the largest share of its
  participants, ties going to the smaller id — because "which circle is this
  mail in" has one answer or none.

The rank on ``MEMBER_OF`` is the archive-wide centrality
:mod:`mailarc_analytics.derived.centrality` has already written to the node,
copied onto the edge so a subgraph read can size a member without a second hop.
That is why the stage order puts ``CENTRALITY`` first: an edge written before
the ranks exist carries a null and nothing recomputes it.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime

from runic.ogm import Session

from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.findings import CommunityFacts, CommunityFindings
from mailarc_analytics.derived.model import (
    Community,
    InCircle,
    MemberOf,
    MessageFacts,
    community_id,
)
from mailarc_analytics.derived.writes import merge_rows
from mailarc_analytics.queries import catalog

logger = logging.getLogger(__name__)

CIRCLE_METHOD = "participants"
"""How ``IN_CIRCLE.score`` was computed — the share of a message's people.

A plain string on the edge rather than an enum, so a graph written by a build
that knows a second way of placing a message still decodes here.
"""

PARTITION_METHOD = "lpa"
"""How the partition was found, stored on the node for the same reason."""


def build_communities(
    facts: Sequence[MessageFacts],
    membership: Mapping[str, int],
    ranks: Mapping[str, float],
    config: AnalyticsConfig,
    *,
    skipped: int = 0,
) -> CommunityFindings:
    """Turn one label-propagation answer into circles worth a node. No I/O.

    *membership* is ``{address id: community number}`` as
    :func:`~mailarc_analytics.derived.algorithms.label_propagation` returns it,
    *ranks* is what ``CENTRALITY`` wrote, and *skipped* is what the §5.1 guard
    stepped over — carried through rather than recomputed, because an empty
    partition from a procedure that threw and an empty partition from an
    archive of hermits are the same ``()`` otherwise.

    A circle smaller than
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.community_min_size`
    is dropped. Two people who write to each other are a correspondence and
    every archive holds thousands of them; three is the smallest number that is
    a circle.
    """
    circles = _circles(membership, config)
    if not circles:
        logger.info("Label propagation found no circle worth a node")
        return CommunityFindings(skipped=skipped)

    placed = _placements(facts, circles, config)
    dated = _dates(facts, placed)
    found = tuple(
        CommunityFacts(
            id=key,
            label=_label(members, ranks),
            method=PARTITION_METHOD,
            members={one: float(ranks.get(one, 0.0)) for one in members},
            messages=placed.get(key, {}),
            first_seen=dated.get(key, (None, None))[0],
            last_seen=dated.get(key, (None, None))[1],
        )
        for key, members in sorted(circles.items())
    )
    logger.info(
        "Found %d circles over %d addresses; %d messages circulate in one",
        len(found),
        sum(len(one.members) for one in found),
        sum(one.message_count for one in found),
    )
    return CommunityFindings(communities=found, skipped=skipped)


def write_communities(session: Session, findings: CommunityFindings) -> None:
    """Write the circles, their members and the mail that circulates in them.

    Communities before their edges, because ``MERGE_MEMBER_OF`` and
    ``MERGE_IN_CIRCLE`` match both endpoints rather than merging them: a circle
    that is not there yet is a bug in this ordering, and merging it would paper
    over that with an empty node — and the other endpoint is an ``Address`` or
    a ``Message``, which this package may never invent.
    """
    merge_rows(
        session,
        catalog.MERGE_COMMUNITIES,
        (
            {
                "id": circle.id,
                "size": circle.size,
                "message_count": circle.message_count,
                "label": circle.label,
                "method": circle.method,
                "first_seen": circle.first_seen,
                "last_seen": circle.last_seen,
            }
            for circle in findings.communities
        ),
        model=Community,
    )
    merge_rows(
        session,
        catalog.MERGE_MEMBER_OF,
        (
            {"address_id": address, "community_id": circle.id, "rank": rank}
            for circle in findings.communities
            for address, rank in sorted(circle.members.items())
        ),
        model=MemberOf,
    )
    merge_rows(
        session,
        catalog.MERGE_IN_CIRCLE,
        (
            {
                "message_id": message,
                "community_id": circle.id,
                "score": score,
                "method": CIRCLE_METHOD,
            }
            for circle in findings.communities
            for message, score in sorted(circle.messages.items())
        ),
        model=InCircle,
    )
    logger.info(
        "Wrote %d communities and %d circle memberships",
        len(findings.communities),
        sum(one.message_count for one in findings.communities),
    )


def _circles(
    membership: Mapping[str, int], config: AnalyticsConfig
) -> dict[str, tuple[str, ...]]:
    """The partition as ``{community id: members}``, small circles dropped.

    Keyed by the digest here rather than later, so the community number stops
    existing at the earliest possible moment and nothing below can accidentally
    take a dependency on it.
    """
    grouped: dict[int, list[str]] = {}
    for address, number in membership.items():
        grouped.setdefault(number, []).append(address)
    return {
        community_id(members): tuple(sorted(members))
        for members in grouped.values()
        if len(members) >= config.community_min_size
    }


def _placements(
    facts: Sequence[MessageFacts],
    circles: Mapping[str, tuple[str, ...]],
    config: AnalyticsConfig,
) -> dict[str, dict[str, float]]:
    """Which circle each message circulates in — at most one, by share.

    The share is over :attr:`~mailarc_analytics.derived.model.MessageFacts.participants`
    — the sender, To, Cc and Bcc — because "this mail circulates here" is a
    claim about everyone who saw it and not only about who was written to
    together. A message with no participants at all is not a question with an
    answer and joins nothing.

    **Walked from the participants and not from the circles**, which is a cost
    rather than a taste: an archive of a hundred thousand messages and a
    thousand circles is a hundred million set intersections the other way
    round, and every one of them but a handful would be empty. A message is
    read once, each of its people is looked up once, and only the circles
    somebody on the message actually belongs to are scored — which is at most
    as many as the message has participants.
    """
    circle_of_address = {
        address: key for key, members in circles.items() for address in members
    }
    placed: dict[str, dict[str, float]] = {key: {} for key in circles}
    for message in facts:
        people = frozenset(message.participants)
        if not people:
            continue
        hits: dict[str, int] = {}
        for person in people:
            key = circle_of_address.get(person)
            if key is not None:
                hits[key] = hits.get(key, 0) + 1
        best: tuple[float, str] | None = None
        for key in sorted(hits):
            share = hits[key] / len(people)
            if share < config.circle_min_share:
                continue
            if best is None or share > best[0]:
                best = (share, key)
        if best is not None:
            placed[best[1]][message.id] = round(best[0], 6)
    return placed


def _dates(
    facts: Sequence[MessageFacts], placed: Mapping[str, Mapping[str, float]]
) -> dict[str, tuple[datetime | None, datetime | None]]:
    """When each circle's mail starts and stops.

    Off the messages placed in it and never off its members, because an address
    has no date. A circle nothing was placed in keeps both ends empty rather
    than borrowing the archive's.
    """
    when = {one.id: one.sent_at for one in facts if one.sent_at is not None}
    found: dict[str, tuple[datetime | None, datetime | None]] = {}
    for key, messages in placed.items():
        dated = sorted(when[message] for message in messages if message in when)
        found[key] = (dated[0], dated[-1]) if dated else (None, None)
    return found


def _label(members: Sequence[str], ranks: Mapping[str, float]) -> str:
    """The circle's name: the most common domain among its members.

    A domain is a name a human recognises and a name nobody invented, which is
    §1.2's rule about what may appear on a derived node. The tie goes to the
    domain of the best-ranked member — and, where two members are ranked
    equally, to the smaller address — so the answer is a function of the
    membership and not of the order it was iterated in.
    """
    counted: dict[str, int] = {}
    for address in members:
        _, _, domain = address.partition("@")
        if domain:
            counted[domain] = counted.get(domain, 0) + 1
    if not counted:
        return ""
    most = max(counted.values())
    tied = {domain for domain, count in counted.items() if count == most}
    if len(tied) == 1:
        return tied.pop()
    best = min(
        (address for address in members if address.partition("@")[2] in tied),
        key=lambda address: (-ranks.get(address, 0.0), address),
    )
    return best.partition("@")[2]
