"""What a tool answers with — the contract a model on the far end reads.

Deliberately *not* the component's own row types. ``CoAddressedRow``,
``TopicRow`` and ``MessageSummary`` are shaped by the statement that produced
them: ``left_id``/``right_id`` says which side of a self-join a value came from,
``eml_sha256`` names a blob only this application can open. Handing those
straight to a language model would publish an internal shape as a public
contract — every later rename inside ``mailarc-analytics`` would silently change
what a model sees, and a field like ``eml_sha256`` costs tokens on every row to
say nothing an outside reader can act on.

So this is an anti-corruption layer, and a thin one: same numbers, names a
reader who has never seen this archive can understand, and nothing that only
makes sense inside the process.

``use_attribute_docstrings`` is on for every model here, which turns the prose
under each field into that field's ``description`` in the JSON schema the client
fetches. That is the whole reason the docstrings are worth writing: the model
decides what to call and how to read the answer from the schema alone, and a
field named ``score`` without a sentence saying "comparable only within one
search kind" is an invitation to sort two result sets together.

Frozen, like every value object in this project: a tool result is passed to a
serialiser and to nothing else, and a mutable answer would let a future caller
edit a finding and hand it on as if the archive had said it.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

CONTRACT = ConfigDict(frozen=True, use_attribute_docstrings=True)
"""One config for all of them — frozen values, docstrings as schema text."""


class MessageHit(BaseModel):
    """One message a search found."""

    model_config = CONTRACT

    message_id: str
    """The archive's own id for the message. Pass it to ``thread`` to read the
    conversation it belongs to."""

    subject: str = ""
    """The subject line as it was sent, empty if the message carried none."""

    sender: str = ""
    """The address it was sent from, empty if the archive could not parse one."""

    sent_at: datetime | None = None
    """When it was sent, as an aware UTC timestamp. ``null`` when the message
    carried no usable ``Date`` header."""

    score: float = 0.0
    """How good this hit is, from 0 to 1, higher is better.

    Comparable **only** against other hits from the same call. Full-text
    relevance and vector similarity are different measurements on different
    scales, so a 0.9 from a full-text search and a 0.9 from a semantic one are
    not the same claim and sorting two result sets into one list invents a
    ranking neither search made.
    """


class SearchAnswer(BaseModel):
    """The hits, plus what the search could not see.

    An object rather than a bare list because of :attr:`notice`. A semantic
    search only reaches messages that already carry a vector, and a message
    without one is *absent from the index* rather than ranked low — so a short
    result over a half-embedded archive is indistinguishable from a thorough
    search of a small one. The sentence is the only thing that tells them apart.
    """

    model_config = CONTRACT

    kind: str
    """Which search answered: ``fulltext`` (the words asked for) or
    ``semantic`` (messages about the subject, including ones that never use the
    word)."""

    hits: tuple[MessageHit, ...] = ()
    """Best first. Empty means the archive holds no message matching the query
    — the search itself worked; every failure is an error, never an empty
    list."""

    notice: str = ""
    """A caveat about this answer's completeness, or empty when there is none.

    Set when a semantic search ran over an archive that is only partly
    embedded. Show it to the user; it is the difference between "your archive
    has nothing on this" and "the embed job has not finished".
    """


class CorrespondentPair(BaseModel):
    """Two addresses that keep being written to in the same message.

    From the derived ``CO_ADDRESSED`` edge, which counts every message that
    addresses both in ``To`` or ``Cc``. ``Bcc`` is deliberately never counted:
    a blind recipient was written to without the others knowing, and a pair
    built on that would publish exactly what the header exists to hide.
    """

    model_config = CONTRACT

    address_a: str
    """One of the two addresses. The pair is unordered — ``a`` is simply the
    lexicographically smaller one, so the same pair always reads the same way."""

    address_b: str
    messages_together: int
    """How many archived messages addressed both of them."""

    first_seen: datetime | None = None
    """The earliest such message; ``null`` if none of them carried a date."""

    last_seen: datetime | None = None
    """The most recent one — the pair may be historical rather than current."""


class TopicCluster(BaseModel):
    """A set of messages an analysis decided belong to one piece of work.

    Derived and disposable: a rebuild deletes every topic and computes them
    again, so a topic id is stable only as long as the archive is.
    """

    model_config = CONTRACT

    topic_id: str
    label: str = ""
    """A human-readable name taken from the members, usually a subject line."""

    joined_by: str = ""
    """Which signal drew the edges in this row — and therefore how much the
    row is worth.

    ``ref`` (a shared ticket or process token), ``thread`` (the provider's own
    conversation), ``subject``, ``attachment`` (the same file by content hash)
    and ``participants`` are **facts**: they are read out of the message
    headers and two messages either share them or they do not. ``embedding``
    is a **suggestion**: two texts came out close together in a vector space,
    which is evidence and not proof. Never present an ``embedding`` cluster to
    a user as an established connection.
    """

    is_suggestion: bool = False
    """``true`` exactly when :attr:`joined_by` is ``embedding``. The same
    distinction as a boolean, so a caller cannot get it wrong by comparing the
    wrong string."""

    messages: int = 0
    """How many messages this signal put into the topic.

    A topic joined by two different signals comes back as two rows, one per
    signal, and the counts are not meant to be added: the same message can be
    held by both.
    """


class MessageTemplate(BaseModel):
    """A text that gets written again and again with barely a word changed.

    Lexical, not semantic: these are messages with the *same wording*, found by
    fingerprinting the body, not messages about the same topic. The point is
    automation — the best candidates are the ones the archive's owner writes.
    """

    model_config = CONTRACT

    template_id: str
    direction: str
    """``sent`` for texts the archive's owner writes, ``received`` for texts
    that keep arriving. Only ``sent`` is automatable; ``received`` says
    something about the sender, not about the reader."""

    occurrences: int = 0
    """How many archived messages carry this text."""

    automation_score: float = 0.0
    """Frequency times regularity times brevity, higher is a better candidate.

    Deliberately not just a count: a short status mail sent on the first of
    every month scores above two hundred identical newsletters, because a
    regular interval is what makes something worth automating.
    """

    sample_text: str = ""
    """The opening of one member message, so a human can recognise the text."""

    first_seen: datetime | None = None
    last_seen: datetime | None = None


class ConversationMessage(BaseModel):
    """One message inside a conversation, in the order it was sent."""

    model_config = CONTRACT

    message_id: str
    subject: str = ""
    sender: str = ""
    sent_at: datetime | None = None
    preview: str = ""
    """The opening of the message with its quoted history and signature
    removed, folded onto one line. Not the full body: a conversation of forty
    messages would otherwise be a megabyte of quoted text, most of it repeated
    from the message above."""


class Conversation(BaseModel):
    """The messages a provider grouped into one conversation, oldest first."""

    model_config = CONTRACT

    thread_id: str = ""
    """The archive's key for the conversation, empty when the message belongs
    to none — some providers thread nothing, and a single mail with no reply is
    a legitimate answer rather than a fault."""

    subject: str = ""
    """The conversation's subject, usually the first message's."""

    messages: tuple[ConversationMessage, ...] = ()
    """Oldest first, so the exchange reads top to bottom."""

    truncated: bool = False
    """``true`` when the conversation holds more messages than were returned.
    Ask again with a larger ``limit`` if the tail matters."""


class TimelineEntry(BaseModel):
    """One message on the archive's timeline, newest first."""

    model_config = CONTRACT

    message_id: str
    sent_at: datetime | None = None
    sender: str = ""
    """The sending address."""

    sender_name: str = ""
    """The display name that address signed itself with in this message; the
    same person often signs differently, so this is per message rather than
    per address."""

    subject: str = ""
    preview: str = ""
    """The opening of the body, quoting and signature removed."""

    labels: tuple[str, ...] = ()
    """What the provider filed it under — the user's own labels first, the
    provider's housekeeping (``INBOX``, ``UNREAD``) last."""

    has_attachments: bool = False
