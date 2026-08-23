"""What an analysis writes back, and the values the analyses pass between them.

Everything declared here is *derived*: deletable and recomputable from the
ground truth at any moment, which is the whole reason it lives in its own
labels and its own package. ``Label`` came from the provider, ``Topic`` came
from us, and the two never mix. Every derived edge carries the ``method`` and
the ``score`` that put it there, so a human can see why it exists and throw it
away without arguing with a header.

The ids are deterministic, and that is a deliberate departure from §5.2 of the
spec, which names ULID for a ``Topic``. A ULID is random, so two rebuilds over
an unchanged archive would write two different graphs — and "running
``rebuild-derived`` twice changes nothing" is the one property phase 5 has to
have. A topic *is* the set of messages in it, so the digest of that set is the
only key under which a rebuild is a no-op; a template *is* its fingerprint plus
the direction it travelled. Neither needs a library, and this environment has
no ULID one anyway.

Nothing here does I/O and nothing here imports anything that does, and
nothing here is an algorithm either: §6 gives ``model.py`` the value objects
and gives every other module one responsibility, so the union-find both A2 and
A3 need sits in :mod:`mailarc_analytics.derived.partition` where either can
import it without importing the other. These declarations describe the schema;
:mod:`mailarc_analytics.queries.catalog` holds every statement that touches
it.

Class order in this file is load-bearing. runic resolves a node's annotations
at declaration time, so every type an annotation names has to exist already —
which is why the four edges stand before the three nodes that name them, and
why :class:`~mailarc_core.archive.model.Message` is imported as a class rather
than referred to by name.
"""

import hashlib
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict
from runic.ogm import Edge, Field, Node, Relation

from mailarc_core.archive.model import Message, to_unsigned_64

_TOPIC_KEY_DIGITS = 32
"""Hex characters of the sha256 a topic id keeps — 128 bits, collision-free
for any archive a desktop holds, and short enough to read in a log line."""


class TopicSignal(StrEnum):
    """The exact signals A2 joins two messages by, strongest first.

    The vocabulary of ``ABOUT.method``, so a reader can tell a fact from a
    suggestion: a ``ref`` cluster is two messages naming the same ticket, a
    ``participants`` cluster is only two messages sent to the same people.
    Phase 6 adds a sixth, semantic one — that is why the edge property is a
    plain string and not this enum, so a graph written by a newer build still
    decodes in an older one.
    """

    REF = "ref"
    THREAD = "thread"
    SUBJECT = "subject"
    ATTACHMENT = "attachment"
    PARTICIPANTS = "participants"


EMBEDDING_METHOD = "embedding"
"""The value of ``ABOUT.method`` for the one signal that is a guess.

Beside :class:`TopicSignal` rather than in it, because the enum is the *exact*
vocabulary and this is deliberately not one of them: an ``embedding`` cluster is
a suggestion, every other method is a fact taken from a header. Written down
once all the same — the composition root stamps it onto an edge and the MCP
server reads it back off one, and two spellings of it would make
``is_suggestion`` quietly false on every row.
"""

SIGNAL_WEIGHTS: Mapping[TopicSignal, float] = MappingProxyType(
    {
        TopicSignal.REF: 1.0,
        TopicSignal.THREAD: 0.8,
        TopicSignal.SUBJECT: 0.5,
        TopicSignal.ATTACHMENT: 0.4,
        TopicSignal.PARTICIPANTS: 0.2,
    }
)
"""How much each signal is worth towards
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.topic_min_score`.

A calibration, not a preference, which is why it sits beside the vocabulary
instead of in the configuration: the five numbers only mean anything relative
to each other and to the threshold. They are chosen so that the first three
carry a topic on their own and the last two carry one only together — a shared
ticket is a project, a shared attachment *and* a shared participant group is a
project, but a shared participant group alone is just two people who talk.
Exposing them as five settings would let a well-meant edit collapse that
distinction and turn a fifth of an archive into one topic.
"""


class TemplateDirection(StrEnum):
    """Which way a template's messages went.

    Sent and received are clustered and reported separately because only what
    you write yourself is automatable (§6.3). A closed set, unlike
    :class:`TopicSignal`, so the node property really is this enum.

    The classification is only ever as good as the archive's account list: it
    asks whether the sender is one of the ``Account`` nodes' addresses, so an
    imported shared mailbox or an alias the user sends under counts its own
    mail as ``RECEIVED`` until that address is archived too.
    """

    SENT = "sent"
    RECEIVED = "received"


def topic_id(member_ids: Iterable[str]) -> str:
    """A topic's key, derived from exactly the messages that are in it.

    Sorted and deduplicated first, so the key cannot depend on the order the
    clustering happened to visit its members in. Union-find picks a root by
    rank, and keying on the root would let a rebuild rename a topic that had
    not changed at all.

    **The key is stable under an unchanged archive and only under one.** One
    message joining a five-message topic changes the digest wholesale: the old
    node is deleted and a differently-keyed one written in its place. That is
    the price of keying on membership, and it is the right price for
    idempotence — but it means a topic id is not a durable reference, and
    nothing outside this package may store one and expect to find it after the
    next import. :func:`template_id` does not have the problem (it keys on the
    representative's fingerprint) and ``Group.id`` does not either (it is the
    ``participant_key``); ``Topic`` is the one derived label whose key is a
    function of its own membership. A user who wants a topic to *last*
    promotes it to a ``Label``, which is ground truth and outlives every
    rebuild.
    """
    joined = "\n".join(sorted(set(member_ids)))
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"topic:{digest[:_TOPIC_KEY_DIGITS]}"


def template_id(representative_simhash: int, direction: TemplateDirection) -> str:
    """A template's key: its representative's fingerprint, and where it went.

    The fingerprint runs through :func:`to_unsigned_64` here rather than at the
    call site, because the graph hands back the *signed* value it had to store
    and ``f"{-5812…:016x}"`` renders a minus sign. Two runs disagreeing about a
    minus sign are not a key. The conversion is idempotent, so a caller that
    already converted loses nothing.

    Two groups in one direction cannot collide over this: equal fingerprints
    are at Hamming distance zero, so they share every band, become candidates
    under any threshold and have already been merged into one group before a
    key is asked for.
    """
    return f"template:{to_unsigned_64(representative_simhash):016x}:{direction.value}"


class MessageFacts(BaseModel):
    """One archived message, reduced to what the three analyses actually read.

    A projection rather than a node, so a whole rebuild holds a few hundred
    bytes per message instead of a live runic entity, a session and a body.

    ``simhash`` is **unsigned** by construction — the reader converts what the
    graph stored, once, so that nothing downstream can band, bucket or render a
    negative value. ``addressed`` and ``participants`` are two different sets on
    purpose: co-addressing is about who was written to *together*, so a Bcc
    belongs nowhere near it, while ``participant_key`` hashes the sender and
    every recipient including Bcc, so a group's size has to be counted over the
    same set or the two disagree about what the key means.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """The canonical id — the ``Message`` node's key."""

    sent_at: datetime | None = None
    subject_norm: str = ""
    participant_key: str = ""

    simhash: int = 0
    """The SimHash of ``body_clean``, unsigned. Zero means an empty body, which
    A3 discards rather than groups: everything empty is trivially alike."""

    refs: tuple[str, ...] = ()
    thread_id: str | None = None
    sender: str = ""

    addressed: tuple[str, ...] = ()
    """To and Cc, sorted and deduplicated. Drives ``CO_ADDRESSED``."""

    participants: tuple[str, ...] = ()
    """Sender, To, Cc **and** Bcc, sorted. Drives ``Group.size``."""

    attachments: tuple[str, ...] = ()
    """The sha256 of every attachment, sorted."""

    outbound: bool = False
    """Whether one of the archive's own accounts sent this."""

    body_clean: str = ""
    """The cleaned body, and the one field a caller may leave empty.

    A3 is the only analysis that needs the text, and only for the messages that
    ended up in a template — a handful out of a hundred thousand. Reading every
    body eagerly puts hundreds of megabytes of Python strings next to an
    in-process FalkorDB, so a rebuild over a large archive is expected to leave
    this empty and fetch the members' bodies afterwards with
    ``catalog.MESSAGE_BODIES``. Small archives can fill it up front and skip
    the second read; both paths produce the same templates.
    """


class CoAddressedPair(BaseModel):
    """Two addresses that were written to on the same message, and how often.

    Unordered in meaning but ordered in storage: ``left`` is always the smaller
    id, so one pair is one edge and a rebuild cannot write it twice under two
    names. Reads must use the undirected pattern.
    """

    model_config = ConfigDict(frozen=True)

    left: str
    right: str
    count: int = 0
    """Messages the two appeared on together — not edges, not recipients."""

    first_seen: datetime | None = None
    last_seen: datetime | None = None


class GroupFacts(BaseModel):
    """One recurring set of correspondents, keyed by its ``participant_key``.

    ``size`` counts the addresses the key was hashed from, not the members of
    this group's message list: the key *is* that set, so any two messages
    sharing a key share a size, and a disagreement means a reader bug rather
    than a bigger group.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """The ``participant_key`` — the ``Group`` node's key."""

    size: int
    message_count: int
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    members: tuple[str, ...] = ()
    """The canonical ids that carry this key, sorted."""


class CorrespondentFindings(BaseModel):
    """Everything A1 found in one pass, plus what it refused to look at.

    The refusal is part of the answer. A message to five hundred people is
    dropped from the pair analysis rather than expanded into a hundred and
    twenty-five thousand edges, and a rebuild that quietly did that would be
    indistinguishable from one that found nothing.
    """

    model_config = ConfigDict(frozen=True)

    pairs: tuple[CoAddressedPair, ...] = ()
    groups: tuple[GroupFacts, ...] = ()
    wide_messages: int = 0
    """Messages whose recipient list exceeded
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.co_addressed_max_recipients`
    and contributed no pair. They still count towards their group."""


class SimilarityEdge(BaseModel):
    """One reason two messages might belong to the same topic.

    The seam phase 6 fills. A2 builds these from the five exact signals itself;
    an embedding KNN can hand in more, applied after the exact ones have
    settled, so a suggestion can only join what a fact left open.
    """

    model_config = ConfigDict(frozen=True)

    left: str
    right: str
    method: str
    weight: float


class TopicMember(BaseModel):
    """What one ``ABOUT`` edge says: why this message is in this topic."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    score: float
    method: str
    """A :class:`TopicSignal` value, or whatever a later phase adds."""


class TopicCluster(BaseModel):
    """A set of messages the exact signals connected, ready to be written.

    ``method`` is the strongest signal that joined anything in the component
    and ``score`` the mean of the members' own scores, so the node ranks while
    the edges stay honest about the individual message.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    label: str = ""
    """The most common non-empty ``subject_norm`` among the members."""

    method: str = ""
    score: float = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    members: tuple[TopicMember, ...] = ()

    @property
    def message_count(self) -> int:
        """How many messages are in it — never stored twice, never disagrees."""
        return len(self.members)


class TopicFindings(BaseModel):
    """Every topic one pass found, and where it had to stop looking.

    The two counters are the honest part. A bucket dropped for being
    boilerplate and a weak pair dropped for want of memory both mean the run
    saw less than the archive holds, and a number a job row can show is the
    difference between a calibration signal and a mystery.
    """

    model_config = ConfigDict(frozen=True)

    clusters: tuple[TopicCluster, ...] = ()
    dropped_buckets: int = 0
    """Signal buckets over
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.topic_bucket_cap`,
    treated as boilerplate rather than as a piece of work."""

    dropped_weak_pairs: int = 0
    """Weak pairs never accumulated, because
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.topic_max_weak_pairs`
    was spent. Only suggestions degrade this way; a ticket or a thread joins
    its messages without ever entering the table."""


class TemplateMember(BaseModel):
    """What one ``INSTANCE_OF`` edge says: how far this copy drifted."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    distance: int
    """Hamming distance to the group's representative, in bits."""


class TemplateGroup(BaseModel):
    """A cluster of near-identical bodies, before anybody has read the text.

    A3 falls in two halves and this is the seam between them: clustering needs
    nothing but fingerprints, while the sample text and the brevity factor need
    the bodies of exactly these members. Handing the ids over first is what
    lets a rebuild read a few hundred bodies instead of a hundred thousand.
    """

    model_config = ConfigDict(frozen=True)

    direction: TemplateDirection
    representative: str
    """The member with the smallest canonical id, whose fingerprint is the
    group's key and the distance every member is measured against."""

    representative_simhash: int
    """That member's SimHash, unsigned."""

    members: tuple[TemplateMember, ...] = ()


class TemplateGrouping(BaseModel):
    """What the clustering half of A3 produced, discards included."""

    model_config = ConfigDict(frozen=True)

    groups: tuple[TemplateGroup, ...] = ()
    unhashable_messages: int = 0
    """Messages whose ``body_clean`` was empty, so their SimHash is zero.

    Discarded before clustering rather than grouped: everything empty is
    trivially alike, and one bucket holding every quoted-only reply in the
    archive would be the highest-scoring finding in it.
    """

    dropped_buckets: int = 0
    """Band buckets left uncompared once
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.template_max_comparisons`
    was spent. Their identical fingerprints are still one template; only the
    near-identical ones went unfound."""

    @property
    def member_ids(self) -> tuple[str, ...]:
        """Every message in a group, sorted — what the body read asks for."""
        return tuple(
            sorted(
                {member.message_id for group in self.groups for member in group.members}
            )
        )


class TemplateCluster(BaseModel):
    """A block of text written over and over, ready to be written back.

    ``automation_score`` is frequency times regularity of the intervals times
    brevity, each in ``0..1``; the formula and why each factor can veto belong
    to :mod:`mailarc_analytics.derived.templates`.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    direction: TemplateDirection
    sample_text: str = ""
    """The shortest member's cleaned body, truncated. Shortest rather than
    first, because the shortest is the one with the least filled in."""

    automation_score: float = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    members: tuple[TemplateMember, ...] = ()

    @property
    def occurrences(self) -> int:
        """How often the text was sent — the node's ``occurrences``."""
        return len(self.members)


class RebuildStage(StrEnum):
    """The five things a rebuild does, in the order it does them."""

    DELETE = "delete"
    READ = "read"
    CORRESPONDENTS = "correspondents"
    TOPICS = "topics"
    TEMPLATES = "templates"


class RebuildProgress(BaseModel):
    """Where a rebuild stands, reported once per stage.

    Once per stage and not more often, because that is the only point at which
    the numbers mean anything: an analysis has read everything or it has not,
    and a half-clustered similarity graph has no percentage to report.
    """

    model_config = ConfigDict(frozen=True)

    stage: RebuildStage
    done: int = 0
    """What the stage produced — nodes deleted, messages read, topics found."""

    total: int = 0
    """What it was working from; zero when the stage has nothing to divide by."""


class DerivedCounts(BaseModel):
    """What one rebuild did, for the log line and for the job row.

    The deletions are counted too, because a rebuild that removed a thousand
    groups and wrote three is a calibration change worth seeing. So is every
    message an analysis declined to use: an archive is allowed to hold mail
    that carries no fingerprint and mail addressed to five hundred people, but
    a rebuild that dropped either without saying so would look exactly like a
    rebuild that found nothing to say.
    """

    model_config = ConfigDict(frozen=True)

    messages: int = 0
    """Messages read out of the ground truth."""

    beyond_ceiling: int = 0
    """Messages
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.max_messages`
    stopped this rebuild from reading at all.

    Zero when there is no ceiling, and zero when the archive is smaller than
    one. A capped rebuild reads a prefix of the canonical id order — reproducible,
    which the idempotence contract needs, but an arbitrary-looking slice of an
    archive all the same, and one nobody would recognise as a sample without
    this number beside it.
    """

    unidentified: int = 0
    """``Message`` nodes the reader skipped for carrying no canonical id."""

    groups: int = 0
    co_addressed: int = 0
    wide_messages: int = 0
    """Messages too widely addressed to contribute a ``CO_ADDRESSED`` pair."""

    topics: int = 0
    dropped_buckets: int = 0
    """Signal buckets A2 treated as boilerplate rather than as a topic."""

    dropped_weak_pairs: int = 0
    """Weak pairs A2 never accumulated, the memory budget having run out."""

    templates: int = 0
    unhashable_messages: int = 0
    """Messages with an empty ``body_clean``, which A3 discards rather than
    groups."""

    dropped_template_buckets: int = 0
    """Band buckets A3's comparison budget could not afford.

    Named apart from :attr:`dropped_buckets` because the two mean opposite
    things about the archive: A2 drops a bucket for being *boilerplate*, A3
    for being too expensive to compare, and a run reporting one is not a run
    reporting the other.
    """

    deleted_nodes: int = 0
    deleted_edges: int = 0


class CoAddressed(Edge, type="CO_ADDRESSED"):
    """Two addresses that get written to together — the only derived edge that
    runs between two ground-truth nodes.

    Which is why it is the one derived thing a rebuild has to delete by edge
    type instead of by taking its node down with it, and why nothing may ever
    ``DETACH DELETE`` an ``Address``.

    Undirected in the spec, and a Cypher edge is not, so the write picks a
    canonical order (lower id first) and every read matches without an arrow.
    runic's own ``relate`` cannot help here: the FalkorDB dialect downgrades
    ``direction="BOTH"`` to ``OUTGOING`` silently, which would eventually store
    both directions of the same pair. The undirected ``MERGE`` in the catalog
    is what keeps it one edge.
    """

    count: int | None = Field(default=None)
    first_seen: datetime | None = Field(default=None)
    last_seen: datetime | None = Field(default=None)


class AddressedGroup(Edge, type="ADDRESSED_GROUP"):
    """This message went to that set of people.

    Deliberately propertyless. Membership is not a judgement with a confidence
    behind it — the message's ``participant_key`` either is the group's key or
    it is not — so there is nothing here for a ``method`` or a ``score`` to
    say. It is declared all the same so all four derived edge types are named
    in one file.
    """


class About(Edge, type="ABOUT"):
    """This message is part of that topic, and here is why.

    Both properties are the point of the edge. ``method`` names the signal that
    drew it, so a ``ref`` edge reads as a fact and a later ``embedding`` edge
    as a suggestion, and ``score`` says how strongly — a human throwing away a
    wrong topic should be able to see what it was built on.
    """

    score: float | None = Field(default=None)
    method: str | None = Field(default=None)
    """A :class:`TopicSignal` value. A plain string rather than the enum so a
    graph written by a build that knows one more signal still decodes here."""


class InstanceOf(Edge, type="INSTANCE_OF"):
    """This message is one copy of that template, this far from it."""

    distance: int | None = Field(default=None)
    """Hamming distance to the template's representative, in bits. Zero means
    the two bodies hash identically, not that they are byte-identical."""


class Group(Node, labels=["Group"]):
    """A set of people who are repeatedly on the same message.

    Keyed by ``participant_key``, so the node needs no clique search: the
    import already hashed the sorted set of everyone on each message, and a
    group is what falls out of grouping by that hash.
    """

    id: str = Field(primary_key=True, index=True)
    size: int | None = Field(default=None)
    """How many addresses the key was hashed from."""

    message_count: int | None = Field(default=None)
    first_seen: datetime | None = Field(default=None)
    last_seen: datetime | None = Field(default=None)

    messages: list[Message] = Relation(
        relationship="ADDRESSED_GROUP",
        direction="INCOMING",
        target=Message,
        edge_model=AddressedGroup,
    )
    """The messages addressed to this group.

    Declared from this side and ``INCOMING``, which emits
    ``(group)<-[:ADDRESSED_GROUP]-(message)`` — the edge the spec draws.
    Declaring it on ``Message`` instead would mean editing ground truth to
    describe something derived, which is the one thing this package exists to
    avoid.
    """


class Topic(Node, labels=["Topic"]):
    """A set of messages the exact signals say belong to one piece of work.

    Never promoted to a ``Label``: a label is the provider's, a topic is ours.
    The user may promote one by hand, and there is no way back.
    """

    id: str = Field(primary_key=True, index=True)
    label: str | None = Field(default=None)
    method: str | None = Field(default=None)
    """The strongest signal that joined this component; see :class:`About`."""

    score: float | None = Field(default=None)
    message_count: int | None = Field(default=None)
    first_seen: datetime | None = Field(default=None)
    last_seen: datetime | None = Field(default=None)

    messages: list[Message] = Relation(
        relationship="ABOUT",
        direction="INCOMING",
        target=Message,
        edge_model=About,
    )


class Template(Node, labels=["Template"]):
    """A text that gets written again and again with barely a word changed.

    Lexical, not semantic: this is the answer to "what do I keep retyping",
    which is a question about wording. Messages about the same *topic* are the
    other analysis and the other node.
    """

    id: str = Field(primary_key=True, index=True)
    sample_text: str | None = Field(default=None)
    occurrences: int | None = Field(default=None)
    automation_score: float | None = Field(default=None)
    direction: TemplateDirection | None = Field(default=None)
    """Sent or received. Kept apart because only what you write yourself is
    worth automating, and a score is only comparable within one direction."""

    first_seen: datetime | None = Field(default=None)
    last_seen: datetime | None = Field(default=None)

    messages: list[Message] = Relation(
        relationship="INSTANCE_OF",
        direction="INCOMING",
        target=Message,
        edge_model=InstanceOf,
    )
