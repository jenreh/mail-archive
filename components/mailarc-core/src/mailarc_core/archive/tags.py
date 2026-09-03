"""The annotation layer: a human's names for sets of messages.

The third thing in the graph, between the ground truth the import writes and
the findings an analysis infers. A ``Tag`` is neither: nothing in a mailbox
produced it, and no rebuild may delete it. It is the same kind of statement as
:attr:`~mailarc_core.archive.model.Address.remote_trusted` — a standing
decision a person made about the archive — and it lives in this package for
that reason and not because it is convenient.

Which makes one rule the whole point of the module: **an analysis may suggest,
a human decides.** ``mailarc-analytics`` computes clusters that are new on every
rebuild (a ``Topic.id`` is a hash of its members), offers them, and writes
``SUGGESTED`` edges pointing at tags. It never writes a ``TAGGED`` edge and
never removes one. Everything that does is here.

Two objects, one behind the other:

``TagRepository``
    Takes a session, like :class:`~mailarc_core.archive.repository.MessageRepository`,
    and is what a caller inside a transaction uses — the worker's auto-accept
    step, for one.
``TagStore``
    Opens a session per call, like :class:`~mailarc_core.archive.reader.ArchiveReader`,
    and is what a page uses. A page should not have to know how a graph is
    opened.

One statement per verb, held as a module constant, and the two that destroy
something are matched against an exact shape at import time. ``untag`` is why:
it removes an edge *between two nodes it must not touch*, so the failure mode
is a ``DETACH DELETE`` that takes the ``Message`` with the membership. That is
not something a repository test would catch — it would pass every assertion and
delete mail — so the statement is read character by character before this module
finishes importing. The device is :mod:`mailarc_core.archive.purge`'s, and the
argument is the same one.
"""

import hashlib
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from runic.ogm import (
    QueryBuilder,
    Repository,
    Session,
    alias,
    count,
    encode_rows,
    param,
    row,
    select,
    unwind,
)

from mailarc_core.archive.model import (
    Message,
    Tag,
    Tagged,
    TagOrigin,
    TagSource,
    TagSummary,
)

logger = logging.getLogger(__name__)

type GraphSessionFactory = Callable[[], AbstractContextManager[Session]]
"""Opens a runic session. Blocking, so callers reach it from a thread."""

TAG_PREFIX = "tag:"
"""What every tag id starts with, so one is recognisable in a mixed listing."""

MEMBER_PAGE = 50
"""How many members one page of a tag hands back."""

_TRANSLITERATED = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "å": "aa", "æ": "ae", "ø": "oe"}
)
"""Letters whose ASCII fold would drop them instead of spelling them.

``unicodedata`` decomposes ``ä`` into ``a`` plus a combining diaeresis, so a
plain fold turns ``Kündigung`` into ``kundigung`` and ``Straße`` into
``strae`` — a name nobody typed. These seven have an established two-letter
form; every other accent folds to its base letter, which is what a reader
expects.
"""

_SEPARATORS = re.compile(r"[^a-z0-9]+")

TAG = alias(Tag, "t")
"""The tag variable every statement here binds — named once, referenced by all.

Rooting at the tag is not a matter of taste. runic emits a predicate naming a
*traversed* variable after the whole pipeline, so ``t.id = $tag`` written from
the message end lands behind the ``DELETE`` it was meant to narrow. Rooted
here it is a root predicate and lands where a reader would put it. The same
argument :attr:`~mailarc_core.archive.model.Account.copies` makes.
"""

MESSAGE = alias(Message, "m")
EDGE = alias(Tagged, "r")


class TagExists(ValueError):
    """A tag with this id is already there — the name slugs to a taken key."""


def tag_id(name: str) -> str:
    """The node key for a tag called *name* — ``tag:<slug>``.

    Derived from the name rather than generated, so two people naming the same
    project the same way get one tag instead of two with half the mail each.
    That is also why a rename does **not** re-key the node: the id is the
    identity, and moving it would orphan every edge already hanging off it.

    A name with no Latin letters in it — ``案件`` — would slug to the bare
    prefix, and every such name would then collide with every other one,
    silently, because ``tag:`` is a perfectly valid key. Those fall back to a
    digest of the name, which is stable and unique for the same reason a
    content hash is.
    """
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("a tag needs a name")
    folded = unicodedata.normalize("NFKD", cleaned.lower().translate(_TRANSLITERATED))
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    slug = _SEPARATORS.sub("-", ascii_only).strip("-")
    if not slug:
        slug = hashlib.sha256(cleaned.lower().encode()).hexdigest()[:16]
    return f"{TAG_PREFIX}{slug}"


# ---------------------------------------------------------------------------
# The statements — one per verb, the destructive two pinned to a shape
# ---------------------------------------------------------------------------

DELETE_TAG: QueryBuilder[Any] = (
    select(TAG)
    .where(TAG.id == param("tag"))
    .delete(detach=True)
    .returning(count("t").as_("removed"))
)
"""Drop one tag and every membership on it.

``DETACH DELETE`` is right here and nowhere else in this module: detaching a
``Tag`` takes that node and the ``TAGGED`` and ``SUGGESTED`` edges incident to
it, and nothing else. No ``Message`` is reachable from the pattern — which is
exactly what :data:`_TAG_DELETE` reads back before this module finishes
importing.
"""

UNTAG: QueryBuilder[Any] = (
    select(TAG)
    .where(TAG.id == param("tag"))
    .traverse(Tag.messages, to=MESSAGE, edge=EDGE)
    .with_(EDGE, MESSAGE, where=MESSAGE.id.in_(param("ids")))
    .delete(EDGE)
    .returning(count("r").as_("removed"))
)
"""Take these messages off this tag, keeping both ends.

``DELETE r`` and never ``DETACH DELETE``: a membership is an edge between two
nodes this module has no business removing — the tag survives an untag, and the
message survives it in every mailbox that holds a copy.

The ``WITH r, m`` stage is load-bearing and not decoration. Without it runic
emits ``m.id IN $ids`` *after* the ``DELETE``, because ``m`` is a traversed
variable — so the statement would empty the whole tag and then filter the rows
it had already destroyed. The stage carries ``m`` forward so the predicate can
stand in front, and :data:`_UNTAG_SHAPE` pins that order.
"""

TAG_MESSAGES: QueryBuilder[Any] = (
    unwind(param("rows"))
    .match(Message, key={Message.id: row("id")}, alias="m")
    .match(Tag, key={Tag.id: row("tag_id")}, alias="t")
    .merge_edge("m", Tagged, "t", alias="r")
    .set({Tagged.source: row("source"), Tagged.at: row("at")}, on="r")
    .returning(count("r").as_("written"))
)
"""Hang these messages on this tag. ``$rows``: ``id``, ``tag_id``, ``source``,
``at``.

``match`` on both endpoints and never ``merge``: a row naming a message that is
not there is a bug in the caller, and merging it would invent an empty
``Message`` carrying nothing but a tag — a node no import can ever reconcile
and every listing would happily show.

The ``SET`` is why :meth:`TagRepository.tag_messages` reads the membership
first. This statement rewrites ``source`` and ``at`` on every row it is handed,
so sending a message that is already tagged would overwrite the decision that
put it there. The filter in front is what keeps the first decision; the
statement itself has no way to know.

Bind ``encode_rows(Tagged, rows)``. ``source`` and ``at`` are declared fields
of the edge, so the enum and the datetime are converted; ``id`` and ``tag_id``
are not, and are passed through untouched — which is what the two ``MATCH``
keys need.
"""

MEMBERSHIP: QueryBuilder[Any] = (
    select(TAG)
    .where(TAG.id == param("tag"))
    .traverse(Tag.messages, to=MESSAGE)
    .with_(MESSAGE, where=MESSAGE.id.in_(param("ids")))
    .project(MESSAGE.id)
)
"""Which of these messages already wear this tag — the read in front of a write.

Narrowed to the ids the caller is about to send rather than reading the whole
tag: a project tag holds thousands of messages and a tagging gesture names five.
"""

MEMBERS: QueryBuilder[Any] = (
    select(TAG)
    .where(TAG.id == param("tag"))
    .traverse(Tag.messages, to=MESSAGE)
    .project(MESSAGE.id)
    .order_by(MESSAGE.sent_at, desc=True)
    .order_by(MESSAGE.id)
    .skip(param("offset"))
    .limit(param("limit"))
)
"""One page of a tag's members, newest first. Ids only — the caller hydrates
them through :class:`~mailarc_core.archive.reader.ArchiveReader`, which is the
one place that knows how to turn a message into a row.

The id tiebreak is what makes the paging deterministic: two messages sent in
the same second would otherwise land in an order the store picks, and a second
page could repeat or skip one of them.
"""

TAGS_OF: QueryBuilder[Any] = (
    select(MESSAGE)
    .where(MESSAGE.id.in_(param("ids")))
    .traverse(Message.tags, to=TAG)
    .project(
        MESSAGE.id.as_("message_id"),
        TAG.id.as_("tag_id"),
        TAG.name,
        TAG.color,
        TAG.origin,
        TAG.created_at,
    )
)
"""The tags on each of these messages — rooted at the message, which is the
end the question comes from.

The one statement here that walks :attr:`~mailarc_core.archive.model.Message.tags`
rather than :attr:`~mailarc_core.archive.model.Tag.messages`. It carries no
predicate on the tag, so nothing is misplaced, and starting at the tag would
mean scanning every one of them to answer about five messages.
"""

LIST_TAGS: QueryBuilder[Any] = (
    select(TAG)
    .traverse(Tag.messages, to=MESSAGE, optional=True)
    .project(
        TAG.id,
        TAG.name,
        TAG.color,
        TAG.origin,
        TAG.created_at,
        count(MESSAGE.id, distinct=True).as_("message_count"),
    )
    .order_by(TAG.name)
)
"""Every tag with the number of messages on it.

``optional=True`` is what keeps a tag whose mail has been cleared out in the
listing: a plain traversal drops it, and a tag with a count of zero is exactly
the one a user needs to see in order to delete it. ``count`` skips the null the
optional match leaves behind, so an empty tag reads ``0`` rather than ``1``.
"""

CLEAR_COLOR: QueryBuilder[Any] = (
    select(TAG)
    .where(TAG.id == param("tag"))
    .set({Tag.color: None}, on=TAG)
    .returning(count("t").as_("cleared"))
)
"""Take the colour off a tag — the one attribute write that needs a statement.

**runic's dirty tracking cannot write a null.** Its update encodes only the
properties that have a value: ``Mapper._encode_props`` skips a field whose value
is ``None`` outright, so ``node.color = None`` followed by a flush emits a
``SET`` that does not mention ``color`` at all and the old one stands. Measured
against a live server before this statement existed — the recolour reported
success and the swatch did not change.

An explicit ``SET t.color = NULL`` is the only way to say it, which is the same
shape ``CLEAR_EMBEDDINGS`` uses in the analytics catalogue and for the same
reason.
"""

READS: dict[str, QueryBuilder[Any]] = {
    "MEMBERSHIP": MEMBERSHIP,
    "MEMBERS": MEMBERS,
    "TAGS_OF": TAGS_OF,
    "LIST_TAGS": LIST_TAGS,
}
"""The statements that answer a question, held together so a guard can sweep
them: none of them may ever contain a ``DELETE``."""

WRITES: dict[str, QueryBuilder[Any]] = {
    "TAG_MESSAGES": TAG_MESSAGES,
    "CLEAR_COLOR": CLEAR_COLOR,
}
"""The statements that change something without destroying it.

Swept at import for the two things that would turn one of them into a
destructive statement without looking like one: a ``DELETE``, and a ``MERGE`` of
a *label* — which would invent an empty ``Message`` or ``Tag`` wherever a row
named one that is not there.
"""


_TAG_DELETE = re.compile(
    r"MATCH \(t:Tag\) WHERE t\.id = \$tag DETACH DELETE t "
    r"RETURN count\(t\) AS removed"
)
"""The only shape the tag deletion may have.

Read character by character, because ``DETACH DELETE`` is the one clause in
this module that can reach a node it did not name. What the shape pins is that
the pattern is a bare ``Tag`` narrowed by ``$tag`` — no traversal, so nothing
but the tag and its own edges is reachable, and no widening can add one without
failing the import.

The layout is normalised first (see :func:`_normalised`), so reformatting is not
mistaken for tampering.
"""

_UNTAG_SHAPE = re.compile(
    r"MATCH \(t:Tag\) WHERE t\.id = \$tag "
    r"MATCH \(t\)<-\[r:TAGGED\]-\(m:Message\) "
    r"WITH r, m WHERE m\.id IN \$ids "
    r"DELETE r RETURN count\(r\) AS removed"
)
"""The only shape the membership deletion may have: ``DELETE`` on the
relationship variable, with the id predicate standing **before** it.

Both halves are the point, and each fails differently. ``DELETE r`` rather than
``DETACH DELETE`` keeps both endpoints — detaching here would take every
message the tag named. And ``WHERE m.id IN $ids`` in front of the ``DELETE`` is
what confines it to the messages asked for: runic places a traversed-variable
predicate behind the write clause, so the shape a naive edit compiles to empties
the tag and reports the right number while doing it.
"""


def _normalised(cypher: str) -> str:
    """Return *cypher* as one line, with the compiler's backticks removed.

    Neither difference carries meaning: runic compiles over several lines and
    backtick-quotes every identifier it emits, so that a model may declare a
    field named after a Cypher keyword. Normalising both away is what lets the
    shapes above stay written as the Cypher a reader would type, and a backtick
    can only wrap an identifier, so dropping it cannot hide a clause.
    """
    return " ".join(cypher.replace("`", "").split())


def _verified(statement: QueryBuilder[Any], shape: re.Pattern[str]) -> None:
    """Raise at import unless *statement* compiles to exactly *shape*.

    Matched against the emitted Cypher rather than against a Python object: a
    statement is a query builder now, and what has to be checked is the text the
    store will run. ``build()`` suffices — neither delete carries a
    dialect-supplied function — and this module refuses to import over anything
    else.
    """
    cypher = statement.build()[0]
    if not shape.fullmatch(_normalised(cypher)):
        raise ValueError(
            f"Refusing a delete statement of an unknown shape: {cypher!r} "
            f"does not match {shape.pattern!r}"
        )


_MERGED_LABEL = re.compile(r"MERGE \(\w*:(\w+)")
"""``MERGE`` on a node pattern — the shape that invents ground truth."""


def _harmless(statements: Mapping[str, QueryBuilder[Any]]) -> None:
    """Raise at import if a read or an upsert has grown a destructive clause.

    Weaker than the two shapes above and deliberately so: what a listing selects
    and what an upsert sets are allowed to change, what either of them
    *destroys* is not.
    """
    for name, statement in statements.items():
        cypher = _normalised(statement.build()[0])
        if "DELETE" in cypher:
            raise ValueError(f"Refusing a non-delete statement that deletes: {name}")
        if _MERGED_LABEL.search(cypher):
            raise ValueError(f"Refusing a statement that merges a node: {name}")


_verified(DELETE_TAG, _TAG_DELETE)
_verified(UNTAG, _UNTAG_SHAPE)
_harmless(READS)
_harmless(WRITES)


class TagRepository(Repository[Tag]):
    """Tags, for a caller that already holds a session.

    Every verb the annotation layer has. The mutating ones report what they
    actually changed rather than what they were asked to change — a rename of a
    tag that is not there answers ``False``, and tagging a message id the
    archive does not hold writes nothing and says so — because the caller is a
    page that has to tell a person what happened.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Tag)

    # -- the tag itself ----------------------------------------------------

    def create(
        self,
        name: str,
        *,
        origin: TagOrigin = TagOrigin.MANUAL,
        color: str | None = None,
    ) -> TagSummary:
        """Add a tag called *name*; raise :class:`TagExists` if the key is taken.

        The lookup in front is what turns a duplicate into a message a person
        can read instead of the constraint violation the graph would otherwise
        raise. It does not *guarantee* uniqueness and is not meant to — two
        sessions can both pass it — which is why the migration puts a ``UNIQUE``
        constraint on ``Tag.id`` behind it.
        """
        key = tag_id(name)
        if self._session.get(Tag, key) is not None:
            raise TagExists(f"a tag keyed {key} already exists")
        node = Tag(
            id=key,
            name=name.strip(),
            color=color,
            origin=origin,
            created_at=datetime.now(UTC),
        )
        self._session.add(node)
        self._session.flush()
        logger.info("Tag %s created from %s", key, origin.value)
        return _summary_of(node)

    def rename(self, tag_id: str, name: str) -> bool:
        """Give the tag a new display name; ``False`` if it is not there.

        The id does not move. It is the identity every ``TAGGED`` edge already
        points at, so re-keying it on a rename would orphan the whole
        membership — which is the one operation this module has no way to undo.
        """
        node = self._session.get(Tag, tag_id)
        if node is None:
            logger.warning("Cannot rename %s — no such tag", tag_id)
            return False
        node.name = name.strip()
        return True

    def recolor(self, tag_id: str, color: str | None) -> bool:
        """Set (or clear) the tag's colour; ``False`` if it is not there.

        The session's dirty tracking carries the change out; the caller's
        session boundary commits it. Same shape as
        :meth:`~mailarc_core.archive.repository.AddressRepository.trust_remote`,
        and for the same reason — this is one attribute of one node, not a
        statement.

        Except for the null, which dirty tracking cannot express: runic's update
        encodes only the properties that have a value, so clearing a colour
        through the attribute alone reports success and changes nothing. The
        attribute is still set — the identity map has to agree with the graph —
        and :data:`CLEAR_COLOR` is what actually writes it.
        """
        node = self._session.get(Tag, tag_id)
        if node is None:
            logger.warning("Cannot recolor %s — no such tag", tag_id)
            return False
        node.color = color
        if color is None:
            self._session.all_rows(CLEAR_COLOR, {"tag": tag_id})
        return True

    def delete(self, tag_id: str) -> bool:
        """Remove the tag and every membership on it; ``False`` if it was gone.

        The messages stay. ``DETACH DELETE`` on a ``Tag`` reaches the edges
        incident to it and nothing beyond them — pinned by :data:`_TAG_DELETE`.
        """
        rows = self._session.all_rows(DELETE_TAG, {"tag": tag_id})
        removed = _first_count(rows, "removed")
        logger.info("Tag %s deleted: %d", tag_id, removed)
        return removed > 0

    # -- membership --------------------------------------------------------

    def tag_messages(
        self,
        tag_id: str,
        ids: Sequence[str],
        *,
        source: TagSource = TagSource.MANUAL,
        at: datetime | None = None,
    ) -> int:
        """Put the tag on these messages. Returns how many memberships are new.

        **Reads what is already tagged and sends only the rest.** The write
        statement sets ``source`` and ``at`` on every row it touches, so sending
        a message that already wears the tag would rewrite the decision that put
        it there — a message tagged by hand in March would come back as
        ``auto``, dated today, the first time an accepted suggestion named it
        again. The filter is the whole mechanism; nothing in the statement can
        express it.

        A message id the archive does not hold writes no edge and is not
        counted: the statement matches both endpoints, so the row simply drops.
        """
        fresh = self._unrecorded(tag_id, ids)
        if not fresh:
            return 0
        stamp = at or datetime.now(UTC)
        rows = [
            {"id": one, "tag_id": tag_id, "source": source, "at": stamp}
            for one in fresh
        ]
        answered = self._session.all_rows(
            TAG_MESSAGES, {"rows": encode_rows(Tagged, rows)}
        )
        written = _first_count(answered, "written")
        logger.info("Tag %s gained %d messages from %s", tag_id, written, source.value)
        return written

    def untag(self, tag_id: str, ids: Sequence[str]) -> int:
        """Take these messages off the tag. Returns how many edges went.

        Neither the tag nor any message is removed — see :data:`UNTAG`.
        """
        asked = list(dict.fromkeys(ids))
        if not asked:
            return 0
        rows = self._session.all_rows(UNTAG, {"tag": tag_id, "ids": asked})
        removed = _first_count(rows, "removed")
        logger.info("Tag %s lost %d messages", tag_id, removed)
        return removed

    def _unrecorded(self, tag_id: str, ids: Sequence[str]) -> list[str]:
        """The asked-for ids that do not already wear the tag, order kept."""
        asked = list(dict.fromkeys(ids))
        if not asked:
            return []
        rows = self._session.all_rows(MEMBERSHIP, {"tag": tag_id, "ids": asked})
        known = {str(one.get("id") or "") for one in rows}
        return [one for one in asked if one not in known]

    # -- reads -------------------------------------------------------------

    def tags_of(self, ids: Sequence[str]) -> dict[str, tuple[TagSummary, ...]]:
        """The tags on each of these messages, keyed by message id.

        A message wearing none is absent rather than present with an empty
        tuple: the caller asked which of its rows carry a tag, and a
        ``dict.get`` answers that without a second population to keep in step.
        The summaries carry no count — see :class:`TagSummary`.
        """
        asked = list(dict.fromkeys(ids))
        if not asked:
            return {}
        found: dict[str, list[TagSummary]] = {}
        for one in self._session.all_rows(TAGS_OF, {"ids": asked}):
            message = str(one.get("message_id") or "")
            if not message:
                continue
            found.setdefault(message, []).append(_summary_of_row(one, key="tag_id"))
        return {
            message: tuple(sorted(summaries, key=lambda each: (each.name, each.id)))
            for message, summaries in found.items()
        }

    def members(
        self, tag_id: str, *, limit: int = MEMBER_PAGE, offset: int = 0
    ) -> tuple[str, ...]:
        """One page of the tag's message ids, newest first."""
        rows = self._session.all_rows(
            MEMBERS, {"tag": tag_id, "limit": limit, "offset": offset}
        )
        return tuple(str(one.get("id") or "") for one in rows if one.get("id"))

    def list_tags(self) -> tuple[TagSummary, ...]:
        """Every tag with its message count, by name."""
        rows = self._session.all_rows(LIST_TAGS)
        return tuple(_summary_of_row(one) for one in rows if one.get("id"))


class TagStore:
    """The annotation layer as a page needs it: one call, one session.

    :class:`TagRepository`'s verbs with the session boundary supplied, the way
    :class:`~mailarc_core.archive.reader.ArchiveReader` supplies it for the
    listing. A page should not have to know how a graph is opened, and a caller
    that already holds a session should not have a second one opened under it —
    which is why both objects exist rather than one.

    Synchronous, like everything else over runic: an async caller wraps a call
    in ``asyncio.to_thread``.
    """

    def __init__(self, graph_session: GraphSessionFactory) -> None:
        self._graph_session = graph_session

    def create(
        self,
        name: str,
        *,
        origin: TagOrigin = TagOrigin.MANUAL,
        color: str | None = None,
    ) -> TagSummary:
        """Add a tag; raises :class:`TagExists` when the name is taken."""
        with self._graph_session() as graph:
            return TagRepository(graph).create(name, origin=origin, color=color)

    def rename(self, tag_id: str, name: str) -> bool:
        with self._graph_session() as graph:
            return TagRepository(graph).rename(tag_id, name)

    def recolor(self, tag_id: str, color: str | None) -> bool:
        with self._graph_session() as graph:
            return TagRepository(graph).recolor(tag_id, color)

    def delete(self, tag_id: str) -> bool:
        with self._graph_session() as graph:
            return TagRepository(graph).delete(tag_id)

    def tag_messages(
        self,
        tag_id: str,
        ids: Sequence[str],
        *,
        source: TagSource = TagSource.MANUAL,
        at: datetime | None = None,
    ) -> int:
        with self._graph_session() as graph:
            return TagRepository(graph).tag_messages(tag_id, ids, source=source, at=at)

    def untag(self, tag_id: str, ids: Sequence[str]) -> int:
        with self._graph_session() as graph:
            return TagRepository(graph).untag(tag_id, ids)

    def tags_of(self, ids: Sequence[str]) -> dict[str, tuple[TagSummary, ...]]:
        with self._graph_session() as graph:
            return TagRepository(graph).tags_of(ids)

    def members(
        self, tag_id: str, *, limit: int = MEMBER_PAGE, offset: int = 0
    ) -> tuple[str, ...]:
        with self._graph_session() as graph:
            return TagRepository(graph).members(tag_id, limit=limit, offset=offset)

    def list_tags(self) -> tuple[TagSummary, ...]:
        with self._graph_session() as graph:
            return TagRepository(graph).list_tags()

    def promote(
        self,
        name: str,
        member_ids: Iterable[str],
        *,
        origin: TagOrigin = TagOrigin.TOPIC,
    ) -> TagSummary:
        """Turn a cluster into a tag: create it and tag its members, one session.

        The one gesture the insights page makes, and the reason the two halves
        are not two calls: a tag created and then left empty because the second
        call failed is worse than no tag at all — it looks like a project whose
        mail has been deleted.

        ``origin`` records *what kind of thing* was promoted and never which
        one. A ``Topic.id`` is a hash of its members and is a different string
        after every rebuild, so storing it would be storing a reference that
        goes stale by design — the tag is the durable reference the cluster is
        not.
        """
        members = list(dict.fromkeys(member_ids))
        with self._graph_session() as graph:
            repository = TagRepository(graph)
            summary = repository.create(name, origin=origin)
            written = repository.tag_messages(
                summary.id, members, source=TagSource.ACCEPTED
            )
        logger.info(
            "Promoted %s to %s with %d messages", origin.value, summary.id, written
        )
        return summary.model_copy(update={"message_count": written})


def _summary_of(node: Tag) -> TagSummary:
    """The projection of a tag we just wrote — counted by nobody, so zero."""
    return TagSummary(
        id=node.id,
        name=node.name or "",
        color=node.color,
        origin=node.origin or TagOrigin.MANUAL,
        created_at=node.created_at,
    )


def _summary_of_row(one: Mapping[str, Any], *, key: str = "id") -> TagSummary:
    """One projected row as a summary, with an unreadable origin tolerated.

    ``origin`` is decoded defensively for the reason the listing filters out
    id-less nodes: a graph that has been around can hold a value a newer build
    wrote, and a listing that raised on one node would take the whole page down
    with it. An unknown one reads as ``MANUAL``, which is the origin that claims
    the least.
    """
    return TagSummary(
        id=str(one.get(key) or ""),
        name=str(one.get("name") or ""),
        color=one.get("color"),
        origin=_origin_of(one.get("origin")),
        created_at=one.get("created_at"),
        message_count=int(one.get("message_count") or 0),
    )


def _origin_of(value: Any) -> TagOrigin:
    """A stored origin as the enum, or ``MANUAL`` with a note in the log."""
    try:
        return TagOrigin(value)
    except ValueError:
        logger.debug("Unknown tag origin %r — reading it as manual", value)
        return TagOrigin.MANUAL


def _first_count(rows: Sequence[Mapping[str, Any]], column: str) -> int:
    """The counter a write returned, or zero when it returned no row."""
    if not rows:
        return 0
    return int(rows[0].get(column) or 0)
