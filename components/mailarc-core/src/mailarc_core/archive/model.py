"""The archive as it stands in the graph: what a node is, what an edge says.

Ground truth and nothing else. Every property below is read out of the message
itself or computed from it deterministically, so importing the same mailbox
twice writes the same values. The nodes an analysis *infers* — groups, topics,
templates — are none of this project's business here: they live in
``mailarc-analytics``, are deleted and recomputed at will, and never mix with
what the provider actually sent.

These are runic OGM models rather than pydantic ones, because declaring them is
how the graph schema gets described. The ``index`` and ``index_type`` arguments
are declarations only — ``runic.migrate`` is what creates the real indexes.

Four plain value objects round it out: :class:`ArchiveSource`, the provenance
of one write, which the message itself cannot know, :class:`ArchiveResult`,
what one write did, and :class:`MessageSummary` with :class:`MessageLabel`,
what one read hands back.

Class order in this file is load-bearing. runic resolves a node's annotations
at declaration time, so every type an annotation names has to exist already.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from runic.ogm import Edge, Field, Node, Relation, Vector

from mailarc_core.mail.model import LabelInfo, LabelKind, MailProvider

_SIGN_BIT = 1 << 63
_UINT64 = 1 << 64


def to_signed_64(value: int) -> int:
    """Reinterpret an unsigned 64-bit value as the signed one a graph stores.

    A SimHash uses all 64 bits, and every Cypher backend's integer is *signed*
    64-bit — so half of all messages carry a value the graph cannot hold. Two's
    complement keeps the bit pattern intact, which is all the template analysis
    needs: it compares Hamming distances, never magnitudes.
    """
    return value - _UINT64 if value >= _SIGN_BIT else value


def to_unsigned_64(value: int) -> int:
    """Undo :func:`to_signed_64` — the same bits, read as unsigned again."""
    return value + _UINT64 if value < 0 else value


class BlobKind(StrEnum):
    """What a blob is, and therefore what suffix it gets on disk."""

    MESSAGE = "eml"
    ATTACHMENT = "bin"


class ArchiveSource(BaseModel):
    """Where one archived copy of a message came from.

    The message cannot know this. The same mail reaching two accounts is *one*
    ``Message`` node with two ``ARCHIVED_FROM`` edges, so everything that
    differs between the two copies — the provider's id for it, the folder it sat
    in, the labels it wore — belongs on the edge or on the account, never on the
    message.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    """The SQLite account row's id as a string; the ``Account`` node's key."""

    account_address: str
    provider: MailProvider
    provider_message_id: str
    provider_thread_id: str | None = None
    folder: str | None = None
    uid: str | None = None
    labels: tuple[LabelInfo, ...] = ()
    archived_at: datetime | None = None
    """When this copy was archived. The writer stamps *now* when it is unset."""


class ArchiveResult(BaseModel):
    """What one archive call did — ``created`` is false on a re-import."""

    model_config = ConfigDict(frozen=True)

    canonical_id: str
    created: bool


class MessageLabel(BaseModel):
    """One label a listed message wears — the name and where it came from.

    Not a :class:`~mailarc_core.mail.model.LabelInfo`: that is the provider's
    description, with the provider's id on it, and the graph keeps neither.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    kind: LabelKind = LabelKind.USER


class MessageSummary(BaseModel):
    """One archived message the way a listing shows it.

    A projection, not a node: what the reader hands out so nothing above the
    archive has to hold a runic entity or a live session. Sender and preview
    are already reduced to the one string a row prints.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """The canonical id — the ``Message`` node's key."""

    sender_name: str = ""
    sender_address: str = ""
    subject: str = ""
    preview: str = ""
    sent_at: datetime | None = None
    has_attachments: bool = False
    eml_sha256: str | None = None
    """The blob the original bytes live under; ``None`` when none was kept."""

    labels: tuple[MessageLabel, ...] = ()
    """What the provider filed it under: a human's labels first, the
    provider's own (INBOX, UNREAD) last, each group by name."""


class Address(Node, labels=["Address"]):
    """One email address, keyed by the normalised form.

    ``display_names`` is a list because the same address signs itself
    differently in every message it sends.
    """

    id: str = Field(primary_key=True)
    local_part: str | None = Field(default=None)
    domain: str | None = Field(default=None, index=True)
    display_names: list[str] = Field(default_factory=list)
    remote_trusted: bool = Field(default=False)
    """Whether the viewer may load this address's remote content without asking.

    The one property on a ground-truth node that a human writes: "always show
    pictures from this sender" is a standing decision about the address, so it
    lives on the address instead of in a second store. The import never sets
    or clears it — the writer leaves existing nodes alone — and an address
    nobody ruled on reads ``False``.
    """

    co_addressed: list["Address"] = Relation(  # noqa: UP037 - see below
        relationship="CO_ADDRESSED", direction="OUTGOING", target="Address"
    )
    """The addresses this one gets written to together with — a *derived* edge.

    Declared here, written elsewhere. Nothing in this component ever creates a
    ``CO_ADDRESSED`` edge: the analytics rebuild computes it from ``SENT_TO``
    and ``COPIED_TO`` and deletes every one of them again on the next run. It
    is declared on a ground-truth node for the same reason
    ``Message.embedding`` and ``Message.embedding_model`` are — a later phase
    fills it in, and a query needs the declaration before it can walk the edge
    at all.

    ``OUTGOING``, although co-addressing means something undirected. **The
    writer orders every pair smaller id first**, and that ordering is the whole
    reason one pair is one edge: hand the same pair in reversed and the graph
    grows a second edge for it. A pattern without an arrow matches each stored
    edge from both ends, so a read either filters ``a.id < b.id`` — which is
    what the top-pairs listing does — or counts everything twice, which is why
    the counting statement carries an arrow.

    The annotation stays a string, and ruff's ``UP037`` is wrong to want it
    unquoted: ``Address`` is not bound yet while its own class body runs and
    runic reads the annotations from ``__init_subclass__``, so the unquoted
    form raises ``NameError`` before the module finishes importing —
    measured, not assumed. The same landmine ``Message.replies_to`` dodges
    with ``Any``; a quoted forward reference survives it here and says more.
    """


class Thread(Node, labels=["Thread"]):
    """A provider conversation, keyed ``{account}:{provider_thread_id}``.

    Scoped to the account on purpose: two providers hand out thread ids from
    their own namespaces and would otherwise collide.
    """

    id: str = Field(primary_key=True)
    subject: str | None = Field(default=None)


class Label(Node, labels=["Label"]):
    """A label the provider gave the message, keyed ``{account}:{name}``.

    Always the provider's own. A topic we inferred is a different node type in
    a different package — a guess never becomes a label.
    """

    id: str = Field(primary_key=True)
    name: str | None = Field(default=None, index=True)
    kind: LabelKind | None = Field(default=None)


class Attachment(Node, labels=["Attachment"]):
    """A file, keyed by its sha256 rather than by its name.

    Content-addressed, so the same file on twenty messages is one node with
    twenty edges and the filename hangs on the edge — the sender renamed it,
    and "who else got this exact file" is a project signal worth having.
    """

    id: str = Field(primary_key=True)
    content_type: str | None = Field(default=None)
    size: int | None = Field(default=None)


class Account(Node, labels=["Account"]):
    """A mailbox this archive imports from, keyed by its SQLite row id."""

    id: str = Field(primary_key=True)
    address: str | None = Field(default=None, index=True)
    provider: MailProvider | None = Field(default=None)


class HasAttachment(Edge, type="HAS_ATTACHMENT"):
    """What *this* message called the attachment and how it carried it."""

    filename: str | None = Field(default=None)
    content_id: str | None = Field(default=None)
    inline: bool = Field(default=False)


class ArchivedFrom(Edge, type="ARCHIVED_FROM"):
    """The provenance of one copy: which account, under which provider id."""

    provider_message_id: str | None = Field(default=None)
    provider_thread_id: str | None = Field(default=None)
    folder: str | None = Field(default=None)
    uid: str | None = Field(default=None)
    archived_at: datetime | None = Field(default=None)


class Message(Node, labels=["Message"]):
    """One mail, keyed by its canonical id — one node however often it arrives.

    ``body_text`` is the full text and feeds full-text search; ``body_clean``
    has the quoted predecessors, the sign-off and the legal footer removed and
    feeds the SimHash and the embedding. Both are needed and they are not
    interchangeable.

    ``embedding`` and ``embedding_model`` are declared here but left empty by
    the import: they are filled in later, by the semantic phase, and that is
    exactly why this writer never overwrites an existing node.

    Separate edge types for ``To``, ``Cc`` and ``Bcc`` rather than one edge with
    a role property: RFC 5322 closes the set, so the co-recipient query needs no
    property filter to walk it. ``SENT_FROM`` rather than ``FROM`` because
    ``FROM`` is awkward in several Cypher dialects.
    """

    id: str = Field(primary_key=True)
    rfc_message_id: str | None = Field(default=None, unique=True)
    subject: str | None = Field(default=None, index_type="FULLTEXT")
    subject_norm: str | None = Field(default=None, index=True)
    sent_at: datetime | None = Field(default=None, index=True)
    body_text: str | None = Field(default=None, index_type="FULLTEXT")
    body_clean: str | None = Field(default=None)
    simhash: int | None = Field(default=None, index=True)
    participant_key: str | None = Field(default=None, index=True)
    refs: list[str] = Field(default_factory=list)
    size_bytes: int | None = Field(default=None)
    has_attachments: bool = Field(default=False)
    eml_sha256: str | None = Field(default=None)
    embedding: Vector | None = Field(default=None, index_type="VECTOR")
    embedding_model: str | None = Field(default=None)

    sender: Address | None = Relation(
        relationship="SENT_FROM", direction="OUTGOING", target="Address"
    )
    recipients: list[Address] = Relation(
        relationship="SENT_TO", direction="OUTGOING", target="Address"
    )
    copied_to: list[Address] = Relation(
        relationship="COPIED_TO", direction="OUTGOING", target="Address"
    )
    blind_copied_to: list[Address] = Relation(
        relationship="BLIND_COPIED_TO", direction="OUTGOING", target="Address"
    )
    thread: Thread | None = Relation(
        relationship="IN_THREAD", direction="OUTGOING", target="Thread"
    )
    replies_to: Any = Relation(
        relationship="REPLIES_TO", direction="OUTGOING", target="Message"
    )
    """The parent message. Annotated ``Any`` because it points at this class.

    runic evaluates every annotation while the class body is still running, and
    ``Message`` is not bound yet at that point. A forward reference here does
    not merely fail for this field — it aborts the whole resolution pass and
    silently strips the datetime and vector converters off every other field on
    the node. ``target`` carries the real type either way.
    """

    labels: list[Label] = Relation(
        relationship="LABELED", direction="OUTGOING", target="Label"
    )
    attachments: list[Attachment] = Relation(
        relationship="HAS_ATTACHMENT",
        direction="OUTGOING",
        target="Attachment",
        edge_model="HasAttachment",
    )
    archived_from: list[Account] = Relation(
        relationship="ARCHIVED_FROM",
        direction="OUTGOING",
        target="Account",
        edge_model="ArchivedFrom",
    )
