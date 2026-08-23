"""A1 — who gets written to together, and which groups keep writing.

Two answers to one question at two resolutions. ``CO_ADDRESSED`` is the pair:
these two addresses appeared on the same message this often. ``Group`` is the
set: this exact circle of people was on this many messages. The pair is a
materialisation of the self-join in §6.1, which gets expensive somewhere around
a hundred thousand messages; the group is free, because the import already
hashed the sorted set of everyone on each message into ``participant_key`` and
a group is what falls out of counting by that hash.

**The sender is not in a pair.** The relation answers "who is written to
together", and the sender is the one addressing rather than one of the
addressed. Including them would make the heaviest edge in every archive "the
user, and everyone the user has ever mailed" — a fact already available as the
degree of ``SENT_FROM``, and one that buries the finding underneath itself.
**Bcc is not in a pair either**, for a stronger reason: a Bcc recipient was
written to *without* the other recipients knowing, and an edge between them
would materialise exactly the confidentiality the header exists to protect into
something a human later reads as "these two work together". Both are in the
group, because ``participant_key`` was hashed over them and a size counted over
anything narrower would disagree with its own key.

``CO_ADDRESSED`` is undirected in the spec and a Cypher edge is not. One
unordered pair becomes exactly one edge, endpoints in ascending id order, and
**every read has to match it without an arrow** —
``(a:Address)-[r:CO_ADDRESSED]-(b:Address)`` — because which way round it was
stored is an accident of who was written to first.

The compute half is a pure function over
:class:`~mailarc_analytics.derived.model.MessageFacts` and needs no graph at
all; the write half is three ``MERGE`` statements out of the catalogue.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from itertools import combinations

from runic.ogm import Session

from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.model import (
    CoAddressedPair,
    CorrespondentFindings,
    GroupFacts,
    MessageFacts,
)
from mailarc_analytics.derived.writes import merge_rows
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import as_graph_datetime

logger = logging.getLogger(__name__)


def build_correspondents(
    facts: Sequence[MessageFacts], config: AnalyticsConfig
) -> CorrespondentFindings:
    """Both halves of A1 over the same facts. No I/O, no graph.

    Two readings of one message — who was on it together, and which circle it
    belongs to — computed separately because they disagree about who counts.
    The pair is To and Cc; the group is the sender and Bcc as well, because
    that is what ``participant_key`` was hashed over. One loop trying to serve
    both would need the distinction anyway and would hide it.
    """
    pairs, wide = _pairs(facts, config)
    groups = _groups(facts, config)
    if wide:
        logger.info(
            "%d messages went to more than %d addresses and contributed no pair",
            wide,
            config.co_addressed_max_recipients,
        )
    logger.info("A1 found %d pairs and %d groups", len(pairs), len(groups))
    return CorrespondentFindings(pairs=pairs, groups=groups, wide_messages=wide)


def write_correspondents(session: Session, findings: CorrespondentFindings) -> None:
    """Write A1's answer back as ``Group``, ``ADDRESSED_GROUP``, ``CO_ADDRESSED``.

    Groups before their edges, because
    :data:`~mailarc_analytics.queries.catalog.MERGE_ADDRESSED_GROUP` matches
    both endpoints rather than merging them: a group that is not there yet is
    a bug in this ordering, and merging it would paper over that with an empty
    node instead of writing no edge.

    Every statement is a ``MERGE``, so running this twice over the same
    findings leaves the same graph. ``session.add`` would not — it compiles to
    ``CREATE``, derived labels carry no unique constraint, and the second run
    would quietly double every node and every edge hung off it.
    """
    merge_rows(
        session,
        catalog.MERGE_GROUPS,
        (
            {
                "id": group.id,
                "size": group.size,
                "message_count": group.message_count,
                "first_seen": as_graph_datetime(group.first_seen),
                "last_seen": as_graph_datetime(group.last_seen),
            }
            for group in findings.groups
        ),
    )
    merge_rows(
        session,
        catalog.MERGE_ADDRESSED_GROUP,
        (
            {"message_id": member, "group_id": group.id}
            for group in findings.groups
            for member in group.members
        ),
    )
    merge_rows(
        session,
        catalog.MERGE_CO_ADDRESSED,
        (
            {
                "left": pair.left,
                "right": pair.right,
                "count": pair.count,
                "first_seen": as_graph_datetime(pair.first_seen),
                "last_seen": as_graph_datetime(pair.last_seen),
            }
            for pair in findings.pairs
        ),
    )
    logger.info(
        "Wrote %d groups and %d co-addressed pairs",
        len(findings.groups),
        len(findings.pairs),
    )


def _pairs(
    facts: Sequence[MessageFacts], config: AnalyticsConfig
) -> tuple[tuple[CoAddressedPair, ...], int]:
    """Every unordered pair of addressed recipients, and how many were skipped.

    The skipped count comes back from here rather than being recounted by the
    caller: two places applying the same threshold is two places to change it
    in, and the one that gets missed reports a number that never happened.

    A message with *k* recipients yields *k(k-1)/2* pairs, so a mail to five
    hundred people is a hundred and twenty-five thousand edges on its own.
    Above the cap the message contributes nothing here — it is a distribution
    list, and "these two were both on the all-hands" is the noise that buries
    the finding rather than the finding. It still counts towards its group,
    where one node absorbs the whole list.
    """
    counts: dict[tuple[str, str], int] = {}
    first: dict[tuple[str, str], datetime] = {}
    last: dict[tuple[str, str], datetime] = {}
    wide = 0
    for message in facts:
        if len(message.addressed) > config.co_addressed_max_recipients:
            wide += 1
            continue
        for pair in combinations(message.addressed, 2):
            counts[pair] = counts.get(pair, 0) + 1
            _widen(first, last, pair, message.sent_at)
    found = tuple(
        CoAddressedPair(
            left=left,
            right=right,
            count=count,
            first_seen=first.get((left, right)),
            last_seen=last.get((left, right)),
        )
        for (left, right), count in sorted(counts.items())
    )
    return found, wide


def _groups(
    facts: Sequence[MessageFacts], config: AnalyticsConfig
) -> tuple[GroupFacts, ...]:
    """One ``Group`` per participant key that clears both thresholds.

    ``size`` comes off the participant set rather than off the member count:
    the key *is* a hash of that set, so every message carrying it describes
    the same circle, and the number of messages is a different question with
    its own field.
    """
    members: dict[str, list[MessageFacts]] = {}
    for message in facts:
        if message.participant_key:
            members.setdefault(message.participant_key, []).append(message)

    found: list[GroupFacts] = []
    for key, group in sorted(members.items()):
        size = _group_size(key, group)
        if size < config.min_group_size or len(group) < config.min_group_messages:
            continue
        dated = [one.sent_at for one in group if one.sent_at is not None]
        found.append(
            GroupFacts(
                id=key,
                size=size,
                message_count=len(group),
                first_seen=min(dated, default=None),
                last_seen=max(dated, default=None),
                members=tuple(sorted(one.id for one in group)),
            )
        )
    return tuple(found)


def _group_size(key: str, group: Sequence[MessageFacts]) -> int:
    """How many addresses this key was hashed from.

    Every member should answer the same number — the key is the hash of the
    set — so a disagreement means either a reader that lost an address or a
    sha256 collision, and both are bugs rather than a bigger group. Reported
    and then resolved by taking the widest set, because a derived layer is
    disposable and one odd key must not cost a whole rebuild: the same reason
    the status panel survives one unreadable graph.
    """
    sizes = {len(one.participants) for one in group}
    if len(sizes) > 1:
        logger.warning(
            "Participant key %s describes sets of %s addresses; taking the widest",
            key[:12],
            sorted(sizes),
        )
    return max(sizes, default=0)


def _widen(
    first: dict[tuple[str, str], datetime],
    last: dict[tuple[str, str], datetime],
    pair: tuple[str, str],
    sent_at: datetime | None,
) -> None:
    """Stretch a pair's window to include this message, if it has a date.

    An undated message still counts towards ``count`` — it happened — but has
    nothing to say about when, and a pair whose members are all undated keeps
    both ends empty rather than inventing one.
    """
    if sent_at is None:
        return
    known = first.get(pair)
    if known is None or sent_at < known:
        first[pair] = sent_at
    known = last.get(pair)
    if known is None or sent_at > known:
        last[pair] = sent_at
