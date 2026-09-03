"""What the four new analyses hand between their compute and their write half.

The same arrangement :mod:`mailarc_analytics.derived.model` describes for A1,
A2 and A3: a pure function returns value objects, a thin write half turns them
into ``$rows``. These six live here rather than beside the older ones for one
reason and it is a house rule — ``model.py`` is at seven hundred lines and the
limit is a thousand, so the half of §5.4 that is *schema* stayed there (the
nodes, the edges, the vocabularies, the id functions) and the half that is
*values* moved here. Nothing in this module imports ``model``, so the split
cannot become a cycle; ``model`` re-exports every name below, so a caller may
keep spelling ``from mailarc_analytics.derived.model import Suggestion``.

Nothing here does I/O, nothing here is an algorithm, and every one of them is
frozen: an analysis passes these between its halves, and a mutable finding is
one a later stage can edit into something the compute half never decided.

Two of them carry a **mapping** rather than a tuple, which is a departure from
:class:`~mailarc_analytics.derived.model.TopicCluster` and its members and is
deliberate. A community's members and its messages each carry one number —
the member's rank, the message's share of the participants — and those numbers
are what ``MEMBER_OF`` and ``IN_CIRCLE`` store. A tuple of ids beside a tuple
of numbers is two lists that can fall out of step; a mapping cannot. Pydantic
does not deep-freeze it, so the mapping a caller hands in is the caller's to
leave alone — build it once and pass it, the way every producer here does.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class GroupingKind(StrEnum):
    """The three shapes a tag suggestion can be argued from, strongest first.

    Also the vocabulary of ``SUGGESTED.method``, so a user accepting a
    suggestion can see what it was made of: a thread is the mail itself saying
    these messages answer each other, a topic is one of A2's clusters, and a
    community is only "these people talk to each other". Ordered here the way
    the weights that use it are ordered, which is the order a reader reads
    them in.
    """

    THREAD = "thread"
    TOPIC = "topic"
    COMMUNITY = "community"


class MessageSignals(BaseModel):
    """What a message says about its own importance, beside its facts.

    A second read rather than five more columns on
    :class:`~mailarc_analytics.derived.model.MessageFacts`: the facts read
    mixes To and Cc into one ``addressed`` set, knows nothing about replies and
    nothing about the provider's labels, and widening it would add five
    cross-multiplying expansions to the statement every rebuild runs over the
    whole archive.

    Every field defaults to the absence of the signal, so a message no row came
    back for is scored on its own properties instead of failing the scorer.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """The canonical id — the ``Message`` node's key."""

    sent_to: tuple[str, ...] = ()
    """``SENT_TO`` alone, sorted. Cc is deliberately not in here: "addressed
    directly" is a claim about the To line, and folding Cc in would make it
    true of every mail sent to a department."""

    reply_count: int = 0
    """Messages that answer this one — the archive's own, not a header count."""

    replied_by: tuple[str, ...] = ()
    """Who answered, sorted. "Replied by you" is this set meeting the
    archive's own addresses."""

    label_names: tuple[str, ...] = ()
    """The provider's label names, sorted. Only Gmail brings ``IMPORTANT`` and
    ``STARRED`` into the graph, so a reason drawn from these is honest only
    where a label really says it."""

    has_attachments: bool = False


class ImportanceScore(BaseModel):
    """How much one message probably matters, and why — never just a number.

    The reasons are the point. A score with no vocabulary behind it is a
    ranking a user cannot argue with, and the whole reason this is arithmetic
    over headers rather than a model is that every term can be named.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str
    score: float = 0.0
    """Clamped to ``0..1`` by the scorer, so a page can render it as a bar."""

    reasons: tuple[str, ...] = ()
    """The fixed vocabulary, sorted — what ``Message.importance_reasons``
    stores."""

    version: str = ""
    """Which scoring run produced it, stamped by the scorer rather than by the
    writer so that a pure test can see it."""


class CommunityFacts(BaseModel):
    """One circle of correspondents, and the mail that circulates inside it.

    Both counts are read off the mappings rather than stored beside them, for
    the reason :class:`~mailarc_analytics.derived.model.TopicCluster` reads its
    own: a stored size can disagree with the edges that were actually written,
    and then the node lies about a number nothing recomputes.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """``community:<digest>`` — see
    :func:`~mailarc_analytics.derived.model.community_id`."""

    label: str = ""
    """The most common domain among the members; a tie goes to the domain of
    the best-ranked address, so the label does not depend on set order."""

    method: str = "lpa"
    """How the partition was found. A plain string, not an enum, so a graph
    written by a build that knows a second method still decodes here."""

    members: Mapping[str, float] = Field(default_factory=dict)
    """Address id → the rank ``CENTRALITY`` gave it. Written as
    ``MEMBER_OF.rank``, which is why the stage order puts centrality first."""

    messages: Mapping[str, float] = Field(default_factory=dict)
    """Message id → the share of its participants that are in this community.
    Written as ``IN_CIRCLE.score``; a message belongs to at most one circle,
    the one with the largest share."""

    first_seen: datetime | None = None
    last_seen: datetime | None = None

    @property
    def size(self) -> int:
        """How many addresses are in it — the node's ``size``."""
        return len(self.members)

    @property
    def message_count(self) -> int:
        """How much mail circulates in it — the node's ``message_count``."""
        return len(self.messages)


class CommunityFindings(BaseModel):
    """Every circle one pass found, and what the guard stepped over.

    The second number is §5.1's whole point. FalkorDB's ``algo.*`` procedures
    throw on a label or a relationship type the graph does not have yet — an
    archive with no ``CO_ADDRESSED`` edge is exactly that — so each call is
    guarded, and a rebuild that skipped one has to say so or it is
    indistinguishable from a rebuild that found nothing.
    """

    model_config = ConfigDict(frozen=True)

    communities: tuple[CommunityFacts, ...] = ()
    skipped: int = 0
    """Procedure calls the guard stepped over; reaches the job row as
    :attr:`~mailarc_analytics.derived.model.DerivedCounts.algorithms_skipped`.
    """


class Suggestion(BaseModel):
    """One message a tag might want, and how strongly the group argues for it.

    Never written to ``TAGGED``: a suggestion is an analysis talking, and the
    annotation layer only records what a human decided. It becomes a
    ``SUGGESTED`` edge, which the next rebuild deletes and recomputes.
    """

    model_config = ConfigDict(frozen=True)

    tag_id: str
    message_id: str
    score: float = 0.0
    """``weight[kind] × k/n`` — the best group's, not the sum of every
    group's, so two weak groups cannot outvote one strong one."""

    method: GroupingKind | str = ""
    """Which kind of group made the winning case."""


class Grouping(BaseModel):
    """A set of messages that already belong together for a reason.

    The input side of the suggestion pass, and the reason it needs no graph:
    a thread, a topic and a community are all just "these ids, for this
    reason", so one function answers for all three and each of them is tested
    without a store.
    """

    model_config = ConfigDict(frozen=True)

    kind: GroupingKind
    members: tuple[str, ...] = ()
    """The message ids in it, sorted and deduplicated by the caller."""
