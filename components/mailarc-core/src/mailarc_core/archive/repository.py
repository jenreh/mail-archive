"""The graph queries the archive needs on top of runic's collection reads.

The counterpart of :mod:`mailarc_core.database.repositories`, for the graph:
``runic.ogm.Repository`` already brings ``find_all``, ``find_all_by_ids``,
``count`` and ``exists``, and none of that is repeated here. What is here is
what a listing and a search cannot express with a primary key: the recent
page, the filtered page, the full-text page, and the order-preserving
hydration a ranked answer needs.

Everything goes through the query builder, never through a Cypher string, so
the statement is checked against the mapped models at build time and the
backend stays :attr:`~mailarc_core.graph.config.GraphConfig.backend`'s choice.
"""

import logging
from datetime import datetime
from typing import Any

from runic.ogm import Repository, Session, alias, count, fulltext_search, score, select

from mailarc_core.archive.model import (
    Account,
    Address,
    Attachment,
    HasAttachment,
    Label,
    Message,
    Thread,
)
from mailarc_core.archive.search import ScoredId, SearchFilters, searchable_terms
from mailarc_core.mail.model import UNTYPED_CONTENT_TYPES

logger = logging.getLogger(__name__)

MESSAGE = alias(Message, "m")
"""The listing's root variable, named once and referenced by both reads.

runic 0.5 replaced ``.alias("m")`` chaining with handles: ``select(MESSAGE)``
names the root, ``MESSAGE.id`` reads ``m.id``, and ``MESSAGE.sender`` supplies
the relation *and* anchors the pattern to ``m``. It also replaces the
``_field()`` detour this module used to carry — a handle's attribute returns a
typed expression, so ``MESSAGE.id.is_not_null()`` type-checks where
``Message.id.is_not_null()`` does not, and a renamed field still fails at
import rather than at query time.
"""

SENDER = alias(Address, "s")
"""The ``SENT_FROM`` target — the same ``s`` the recent listing binds.

One variable for both jobs on purpose: when a sender filter is set the
traversal that *filters* is the traversal the listing *shows*, so the two can
never disagree about which address a row's sender column came from.
"""

RECIPIENT = alias(Address, "r")
"""The recipient walk's target, reached over ``SENT_TO`` **and** ``COPIED_TO``.

One alternation pattern rather than two traversals: "sent or copied to" is one
walk, and two separate patterns would double-count a message that did both.
``BLIND_COPIED_TO`` is deliberately out — Bcc is what the sender hid, and a
search field should not un-hide it as a side effect.

One variable is enough for the two reads that bind it — the *any* half of
:func:`_addressed` and :meth:`AddressRepository.find_recipients`. The *all*
half cannot use it: it needs one variable per address, so it names its own
``r0``, ``r1``, … beside this one.
"""

ACCOUNT = alias(Account, "a")
"""The ``ARCHIVED_FROM`` target — which mailbox imported this copy."""

THREAD = alias(Thread, "t")
"""The ``IN_THREAD`` target — the conversation a message sits in.

``t`` because ``m``, ``s``, ``r`` and ``a`` are taken above and a grouped
listing reads one statement against another: the same letter meaning two
things across two reads is how a join stops being checkable by eye.
"""

ATTACHMENT = alias(Attachment, "f")
"""The ``HAS_ATTACHMENT`` target — a file on a message.

``f`` and not ``a``, which the account already holds; see :data:`THREAD` for
why one letter may mean one thing across this module.
"""

ATTACHED = alias(HasAttachment, "h")
"""The ``HAS_ATTACHMENT`` edge itself, bound wherever the file is.

The node is content-addressed, so everything *this* message called the file —
its name, its Content-ID — hangs on the edge and nowhere else. Both reads
below need it: one to tell a file from a signature logo, the other to match a
shrug by the only thing that says what it is.
"""


def _carried(statement: Any) -> Any:
    """Narrow a bound ``HAS_ATTACHMENT`` to the parts a reader is shown.

    The one spelling of *this is a file*, so the picker and the filter cannot
    drift: a part the HTML pulls in by ``cid:`` and the sender never named is
    a logo in a signature, and no mail client shows a paperclip for it. That
    is
    :attr:`~mailarc_core.mail.model.ParsedAttachment.embedded` verbatim —
    inverted, because a statement asks for what it keeps — and it is what
    ``Message.has_attachments`` was counted from at import.
    """
    return statement.where(
        ATTACHED.filename.is_not_null() | ATTACHED.content_id.is_null()
    )


_UNTYPED = sorted(UNTYPED_CONTENT_TYPES)
"""The shrug types as a parameter list — sorted so the statement is stable.

A list and not the set, because a Cypher ``IN`` takes one, and sorted so two
runs build the same string and the backend can reuse its plan.
"""

_RECIPIENT_TYPES = ["SENT_TO", "COPIED_TO"]


class MessageRepository(Repository[Message]):
    """Messages, the way a listing wants them: newest first, sender attached.

    Both reads keep to nodes that carry a canonical id. A ``Message`` without
    one is not something the writer produces, but a graph that has been
    around — a smoke test, an older schema — can hold such a node, and a
    listing that trips over it would take the whole page down with it.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Message)

    def count(self) -> int:
        """How many archived messages there are — the listing's total."""
        return self._session.count(select(MESSAGE).where(MESSAGE.id.is_not_null()))

    def find_recent(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[tuple[Message, Address | None]]:
        """The newest ``limit`` messages after ``offset``, each with its sender.

        One traversal rather than a lazy relation per row: a listing of fifty
        messages is the textbook N+1. The traversal is optional, so a message
        whose ``From`` could not be parsed still lists — with ``None`` beside
        it. Ordered by ``sent_at`` descending; a message without a date sorts
        wherever the backend puts nulls.
        """
        statement = (
            select(MESSAGE)
            .where(MESSAGE.id.is_not_null())
            .traverse(MESSAGE.sender, to="s", optional=True)
            .order_by(MESSAGE.sent_at, desc=True)
            .skip(offset)
            .limit(limit)
            .return_nodes(MESSAGE, "s")
        )
        rows = self._session.all_with_edges(statement)
        logger.debug("Listed %d messages from offset %d", len(rows), offset)
        return [(message, sender) for message, sender in rows]

    def find_labels(self, ids: list[str]) -> dict[str, list[Label]]:
        """The labels on each of these messages, keyed by message id.

        A second statement rather than a second traversal on the listing: a
        message wears several labels, and a row per ``(message, label)`` pair
        would make the listing's ``LIMIT`` count labels instead of messages.
        One ``IN`` over the page's ids keeps it at one read per page. A
        message without labels is simply absent from the answer.
        """
        if not ids:
            return {}
        statement = (
            select(MESSAGE)
            .where(MESSAGE.id.in_(ids))
            .traverse(MESSAGE.labels, to="l", optional=False)
            .return_nodes(MESSAGE, "l")
        )
        found: dict[str, list[Label]] = {}
        for message, label in self._session.all_with_edges(statement):
            found.setdefault(message.id, []).append(label)
        logger.debug("Read labels for %d of %d messages", len(found), len(ids))
        return found

    def find_recipients(self, ids: list[str]) -> dict[str, list[Address]]:
        """The ``To`` addresses of each of these messages, keyed by message id.

        :meth:`find_labels`'s shape for the same reason: a message has several
        recipients, so the edge is walked in a statement of its own with one
        ``IN`` over the page's ids. ``SENT_TO`` alone and never the
        :data:`RECIPIENT` alternation — a listing grouped "by receiver" means
        who the mail was *to*, and folding Cc in would file a message under
        whoever happened to be copied. A message that names nobody is absent.
        """
        if not ids:
            return {}
        statement = (
            select(MESSAGE)
            .where(MESSAGE.id.in_(ids))
            .traverse(MESSAGE.recipients, to=RECIPIENT, optional=False)
            .return_nodes(MESSAGE, RECIPIENT)
        )
        found: dict[str, list[Address]] = {}
        for message, address in self._session.all_with_edges(statement):
            found.setdefault(message.id, []).append(address)
        logger.debug("Read recipients for %d of %d messages", len(found), len(ids))
        return found

    def find_filtered(
        self, filters: SearchFilters, *, limit: int = 50, offset: int = 0
    ) -> list[tuple[Message, Address | None]]:
        """The messages the structured filters leave, newest first, with sender.

        :meth:`find_recent` with narrowing: same row shape, same ordering,
        same optional sender — plus one ``MATCH`` per set filter. ``DISTINCT``
        goes on before the page cut because the recipient alternation fans
        out: a message reaching two matching addresses is two rows, and a
        ``LIMIT`` counting rows instead of messages would hand back short
        pages that look like a small archive. The id tiebreak keeps two runs
        of the same page the same page — ``sent_at`` ties are real (mailing
        list bursts land on one second).
        """
        statement = _filtered(
            select(MESSAGE).where(MESSAGE.id.is_not_null()),
            filters,
            listing=True,
        )
        statement = (
            statement.order_by(MESSAGE.sent_at, desc=True)
            .order_by(MESSAGE.id)
            .distinct()
            .skip(offset)
            .limit(limit)
            .return_nodes(MESSAGE, SENDER)
        )
        rows = self._session.all_with_edges(statement)
        logger.debug(
            "Filtered listing matched %d rows from offset %d", len(rows), offset
        )
        return [(message, sender) for message, sender in rows]

    def count_filtered(self, filters: SearchFilters) -> int:
        """How many messages the structured filters leave — the page's total.

        ``count(DISTINCT m.id)`` rather than the session's ``count(*)``,
        for the same fan-out reason :meth:`find_filtered` deduplicates: the
        recipient alternation can match one message twice, and the total must
        count messages, not rows.
        """
        statement = _filtered(
            select(MESSAGE).where(MESSAGE.id.is_not_null()),
            filters,
            listing=False,
        ).project(count(MESSAGE.id, distinct=True).as_("total"))
        rows = self._session.all_rows(statement)
        return int(rows[0].get("total") or 0) if rows else 0

    def search_fulltext(
        self, filters: SearchFilters, *, limit: int = 50, offset: int = 0
    ) -> list[ScoredId]:
        """Full-text matches for ``filters.text``, narrowed and paged, best first.

        The proven shape from analytics' ``FULLTEXT_MESSAGES``: the procedure
        call, then a ``WHERE`` **after** the ``YIELD`` — the index cannot be
        narrowed before the fact, so every structured filter is applied to
        what it produced. Only ids and scores come back; hydration is
        :meth:`find_by_ids`'s job, so a page of hits is two statements
        however many filters are set.

        The text is reduced to plain words first —
        :func:`~mailarc_core.archive.search.searchable_terms` raises
        :class:`ValueError` when nothing survives — because the bound
        parameter protects the Cypher, not the RediSearch expression inside
        it. The relevance is the index's raw score; the id tiebreak makes
        pagination deterministic when scores tie, and it orders the *projected*
        column because ``m`` is out of scope after a ``DISTINCT`` projection.
        """
        terms = searchable_terms(filters.text)
        statement = fulltext_search(MESSAGE, query=terms).where(
            MESSAGE.id.is_not_null() & (MESSAGE.id != "")
        )
        statement = _filtered(statement, filters, listing=False)
        statement = (
            statement.project(MESSAGE.id, score().as_("relevance"))
            .order_by(score().as_("relevance"), desc=True)
            .order_by("id")
            .distinct()
            .skip(offset)
            .limit(limit)
        )
        rows = self._session.all_rows(statement)
        logger.debug(
            "Fulltext %r matched %d rows from offset %d", terms, len(rows), offset
        )
        return [
            ScoredId(
                id=str(row.get("id") or ""),
                relevance=float(row.get("relevance") or 0.0),
            )
            for row in rows
        ]

    def find_by_ids(self, ids: list[str]) -> list[tuple[Message, Address | None]]:
        """These messages, each with its sender, **in the order asked for**.

        The hydration half of every ranked answer — full-text here, semantic
        in the analytics component — so the caller's order *is* the ranking
        and must survive. The graph answers ``IN`` in whatever order it
        pleases; the rows are re-sorted by the input, and an id the graph no
        longer holds is simply absent rather than a hole. Empty in, empty
        out, without a round trip.
        """
        if not ids:
            return []
        statement = (
            select(MESSAGE)
            .where(MESSAGE.id.in_(ids))
            .traverse(MESSAGE.sender, to=SENDER, optional=True)
            .return_nodes(MESSAGE, SENDER)
        )
        found: dict[str, tuple[Message, Address | None]] = {
            message.id: (message, sender)
            for message, sender in self._session.all_with_edges(statement)
        }
        return [found[one] for one in ids if one in found]


class ThreadRepository(Repository[Thread]):
    """Conversations, the way a grouped listing needs them.

    Three reads, and they answer three different questions: which conversation
    each message of a page sits in, how big each of those conversations really
    is, and — when a reader asks for it — which messages one of them holds.

    Separate from :class:`MessageRepository` because it is a different root.
    That repository owns the listing, the filter, the full-text page and the
    hydration, all rooted at ``m``; two of the three reads here are rooted at
    ``t``, and that is not a stylistic choice — see :meth:`count_members`.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(session, Thread)

    def find_for_messages(self, ids: list[str]) -> dict[str, str]:
        """Which conversation each of these messages was filed under.

        :meth:`MessageRepository.find_labels` with a different edge, and for
        the same reason: one ``IN`` over the page's ids rather than a lazy
        relation per row. A message in no thread is simply absent from the
        answer, the way a message without labels is.

        **A message can carry more than one ``IN_THREAD`` edge.** The writer
        keys a ``Thread`` per account, and the same mail archived from two
        mailboxes is deduplicated to one ``Message`` with an edge to each
        account's thread. The **lexicographically smallest** thread id wins,
        which is
        :func:`mailarc_analytics.derived.reader._first`'s rule and is chosen
        for the same reason: the store makes no promise about the order rows
        come back in, and the only property that matters here is that the same
        message lands in the same group whichever page brought it and however
        the driver felt like ordering the rows.

        Where two members of one exchange share no account, their smallest
        keys differ and they land in two groups. That is deterministic and
        wrong in the same way every time; joining them would need a
        conversation node that crosses accounts, which is the analytics
        component's job and not this one's.
        """
        if not ids:
            return {}
        statement = (
            select(MESSAGE)
            .where(MESSAGE.id.in_(ids))
            .traverse(MESSAGE.thread, to=THREAD, optional=False)
            .return_nodes(MESSAGE, THREAD)
        )
        found: dict[str, str] = {}
        for message, thread in self._session.all_with_edges(statement):
            here = found.get(message.id)
            if here is None or thread.id < here:
                found[message.id] = thread.id
        logger.debug("Read conversations for %d of %d messages", len(found), len(ids))
        return found

    def count_members(self, thread_ids: list[str]) -> dict[str, int]:
        """How many messages each of these conversations really holds.

        Rooted at the thread, and that is the whole point of declaring
        :attr:`~mailarc_core.archive.model.Thread.messages`. runic emits a
        predicate naming a *traversed* variable after the whole pipeline, so
        the message-rooted spelling would expand every message in the archive
        and filter afterwards; rooted here the ``IN`` lands on the primary key
        before anything is expanded.

        ``count(DISTINCT m.id)`` rather than ``count(*)`` because a member
        archived from two accounts reaches this thread once but is one message,
        and the number a header prints has to count messages.
        """
        if not thread_ids:
            return {}
        statement = (
            select(THREAD)
            .where(THREAD.id.in_(thread_ids))
            .traverse(Thread.messages, to=MESSAGE, optional=False)
            .project(
                THREAD.id.as_("thread_id"),
                count(MESSAGE.id, distinct=True).as_("total"),
            )
        )
        rows = self._session.all_rows(statement)
        return {
            str(row.get("thread_id") or ""): int(row.get("total") or 0)
            for row in rows
            if row.get("thread_id")
        }

    def find_members_for(
        self, thread_ids: list[str], *, limit: int = 200
    ) -> dict[str, list[str]]:
        """The ids of several conversations' messages at once, newest first.

        :meth:`find_members` for a whole page of groups. A grouped listing
        wants every conversation it draws to be *whole*, and asking per thread
        would be one round trip per group — the N+1 :meth:`find_labels` exists
        to avoid, with a graph read on the end of it.

        **Rooted at the thread**, like :meth:`count_members` and for the same
        reason: the ``IN`` then lands on the primary key before anything is
        expanded, where the message-rooted spelling would expand every message
        in the archive and filter afterwards.

        Ordered in Python rather than in Cypher. The statement returns one row
        per ``(thread, message)`` pair and a ``LIMIT`` over that would cut the
        *page*, not each conversation; sorting the rows of each thread here
        keeps ``limit`` meaning what :meth:`find_members` means by it — how
        much of one conversation comes back. A member without a date sorts
        last, which is where the listing puts it too.
        """
        if not thread_ids:
            return {}
        statement = (
            select(THREAD)
            .where(THREAD.id.in_(thread_ids))
            .traverse(Thread.messages, to=MESSAGE, optional=False)
            .project(
                THREAD.id.as_("thread_id"),
                MESSAGE.id.as_("id"),
                MESSAGE.sent_at.as_("sent_at"),
            )
            .distinct()
        )
        found: dict[str, list[tuple[datetime | None, str]]] = {}
        for row in self._session.all_rows(statement):
            thread_id = str(row.get("thread_id") or "")
            message_id = str(row.get("id") or "")
            if not thread_id or not message_id:
                continue
            found.setdefault(thread_id, []).append((row.get("sent_at"), message_id))
        logger.debug(
            "Read the members of %d of %d conversations", len(found), len(thread_ids)
        )
        return {
            thread_id: [one for _, one in _newest_first(members)][:limit]
            for thread_id, members in found.items()
        }

    def find_members(
        self, thread_id: str, *, limit: int = 200, offset: int = 0
    ) -> list[str]:
        """The ids of one conversation's messages, newest first.

        Ids and nothing else, so hydration is
        :meth:`MessageRepository.find_by_ids`'s job — the same division every
        ranked answer already makes. That is not tidiness: attaching the
        display sender here would mean an ``OPTIONAL MATCH`` beside a ``WHERE``
        that names a traversed variable, which is precisely the misplacement
        :func:`_filtered` exists to document. One more statement on a path that
        runs once per button press buys the whole class of bug away.

        Ordered like the listing it feeds, id tiebreak included, so a
        conversation reads the same way round as the page it expands into.
        """
        if not thread_id:
            return []
        statement = (
            select(MESSAGE)
            .traverse(MESSAGE.thread, to=THREAD, optional=False)
            .where(THREAD.id == thread_id)
            .order_by(MESSAGE.sent_at, desc=True)
            .order_by(MESSAGE.id)
            .distinct()
            .skip(offset)
            .limit(limit)
            .project(MESSAGE.id.as_("id"))
        )
        rows = self._session.all_rows(statement)
        found = [str(row.get("id") or "") for row in rows]
        logger.debug("Conversation %s holds %d listed messages", thread_id, len(found))
        return [one for one in found if one]


class AttachmentRepository(Repository[Attachment]):
    """Files: what the search form's file filter can be narrowed by."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, Attachment)

    def find_content_types(self) -> list[str]:
        """Every content type an archived file *states*, commonest first.

        Counted rather than merely collected, for the reason
        :meth:`AddressRepository._used` gives: the picker above this offers a
        bounded list, so the types worth offering are the ones the archive
        actually holds a lot of. ``DISTINCT`` inside the count because one
        file content is one node with an edge per message that carried it —
        the same PDF on twenty mails is one type sighting worth twenty, not
        one.

        The type tiebreak keeps two runs of the same archive in the same
        order; without it two equally common types could swap between page
        loads and a picker would reshuffle under a reader.

        Two things are left out and both are the same decision — the picker
        must offer only what a reader would call a file. A ``cid:`` logo is
        counted out by :func:`_carried`, and a content type that states
        nothing is counted out here so it cannot become one option standing
        for a folder of unrelated files; those come back from
        :meth:`find_untyped_names` under the only thing that describes them.
        """
        messages = count(MESSAGE.id, distinct=True).as_("messages")
        statement = _carried(
            select(MESSAGE)
            .where(MESSAGE.id.is_not_null())
            .traverse(MESSAGE.attachments, to=ATTACHMENT, edge=ATTACHED, optional=False)
        ).where(
            ATTACHMENT.content_type.is_not_null()
            & ATTACHMENT.content_type.not_in_(_UNTYPED)
        )
        statement = (
            statement.project(ATTACHMENT.content_type.as_("content_type"), messages)
            .order_by(messages, desc=True)
            .order_by("content_type")
        )
        rows = self._session.all_rows(statement)
        logger.debug("The archive holds %d stated attachment types", len(rows))
        return [key for row in rows if (key := str(row.get("content_type") or ""))]

    def find_untyped_names(self) -> list[str]:
        """The names of the files whose sender stated no type, commonest first.

        The other half of :meth:`find_content_types`, and the reason it is a
        second read rather than a wider first one: a name is only worth
        carrying where the type says nothing, and asking for every filename in
        the archive would hand a picker one option per attachment.

        Counted and ordered exactly as the types are, so the two lists can be
        merged into one menu without one of them arriving unsorted. A part
        with no name at all cannot be matched by name and is therefore not
        offered — which is the same bargain, kept in the one place a broken
        one would show.
        """
        messages = count(MESSAGE.id, distinct=True).as_("messages")
        statement = _carried(
            select(MESSAGE)
            .where(MESSAGE.id.is_not_null())
            .traverse(MESSAGE.attachments, to=ATTACHMENT, edge=ATTACHED, optional=False)
        ).where(ATTACHED.filename.is_not_null() & ATTACHMENT.content_type.in_(_UNTYPED))
        statement = (
            statement.project(ATTACHED.filename.as_("filename"), messages)
            .order_by(messages, desc=True)
            .order_by("filename")
        )
        rows = self._session.all_rows(statement)
        logger.debug("The archive holds %d files nobody typed", len(rows))
        return [name for row in rows if (name := str(row.get("filename") or ""))]


class AddressRepository(Repository[Address]):
    """Addresses: what the search form can offer, and remote-content trust."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, Address)

    def find_senders(self) -> list[str]:
        """Every address an archived message came *from*, busiest first."""
        return self._used(
            select(MESSAGE)
            .where(MESSAGE.id.is_not_null())
            .traverse(MESSAGE.sender, to=SENDER, optional=False),
            SENDER,
        )

    def find_recipients(self) -> list[str]:
        """Every address an archived message went *to*, busiest first.

        ``SENT_TO`` and ``COPIED_TO``, the alternation the recipient filter
        walks — an address that only ever appears in a ``Bcc`` is not offered,
        because filtering by it is not offered either.
        """
        return self._used(
            select(MESSAGE)
            .where(MESSAGE.id.is_not_null())
            .traverse(
                MESSAGE.recipients,
                to=RECIPIENT,
                types=_RECIPIENT_TYPES,
                optional=False,
            ),
            RECIPIENT,
        )

    def _used(self, statement: Any, target: Any) -> list[str]:
        """The addresses one traversal reaches, most-used first.

        Counted rather than merely collected, and that is what the order is
        for: the picker above this shows a bounded number of options before
        anybody types, so the ones worth showing are the ones the archive
        actually turns on. ``DISTINCT`` inside the count because the
        recipient alternation reaches one message twice — to *and* copied —
        and that is one message, not two.

        The id tiebreak keeps two runs of the same archive in the same order;
        without it two addresses on the same count could swap between page
        loads and a list would reshuffle under a reader.
        """
        messages = count(MESSAGE.id, distinct=True).as_("messages")
        statement = (
            statement.where(target.id.is_not_null() & (target.id != ""))
            .project(target.id.as_("address"), messages)
            .order_by(messages, desc=True)
            .order_by("address")
        )
        rows = self._session.all_rows(statement)
        logger.debug("The archive holds %d addresses in this role", len(rows))
        return [key for row in rows if (key := str(row.get("address") or ""))]

    def is_remote_trusted(self, address: str) -> bool:
        """Whether this sender's remote content may load without asking.

        An address the archive has never seen answers ``False`` — there is
        nothing to hang a decision on, and the safe answer is the default one.
        """
        node = self._session.get(Address, _address_key(address))
        return node is not None and node.remote_trusted

    def trust_remote(self, address: str) -> bool:
        """Record the decision on the node; ``False`` if the node is missing.

        The session's dirty tracking carries the change out; the caller's
        session boundary commits it.
        """
        node = self._session.get(Address, _address_key(address))
        if node is None:
            logger.warning("Cannot trust %s — the archive has no such address", address)
            return False
        node.remote_trusted = True
        logger.info("Remote content allowed for %s from now on", node.id)
        return True


def _address_key(address: str) -> str:
    """The node key form — lowercased, trimmed, as ``EmailAddress`` builds it."""
    return address.strip().lower()


def _filtered(statement: Any, filters: SearchFilters, *, listing: bool) -> Any:
    """Every structured filter, appended in the one order that keeps them filters.

        The order is load-bearing, and it is about how runic places ``WHERE``:
        every predicate that names a traversed variable is emitted as **one**
        clause after the **last** pattern clause. Cypher attaches a ``WHERE`` to
        the clause before it — and on an ``OPTIONAL MATCH`` that *nullifies* the
        optional bindings instead of dropping the row. So the listing's optional
        display-sender traversal goes **first**, the filter traversals (all
        plain ``MATCH``) after it, and the collected predicates land on a clause
        that drops. Reorder this and every filter silently stops filtering the
        listing; ``test_archive_search.py`` pins the emitted clause order.

        That order needs one seam to be legal at all: FalkorDB refuses a plain
        ``MATCH`` that follows an ``OPTIONAL MATCH`` — *"A WITH clause is required
        to introduce a MATCH clause after an OPTIONAL MATCH"* — so a ``WITH m, s``
        closes the optional half whenever a filter traversal is going to follow
        it. It carries the display sender across on purpose: after a ``WITH`` only
        the named variables stay in scope, and ``s`` is what the row shows.

        ``listing`` is whether the caller shows rows and therefore needs
        :data:`SENDER` bound even when no sender filter is set. With a sender
        filter the display traversal and the filter traversal are the *same*
        non-optional one — a message whose ``From`` could not be parsed cannot
        match a sender filter anyway.

        Senders and recipients match **exactly** on the normalised address, the way
        the account does, because all three come from a picker rather than a text
        field: the form offers what the archive holds. Containment would be the
        wrong reading of a picked value — ``anna@example.com`` is a substring of
        ``hanna@example.com`` — so the address is lowercased like the node keys
        and compared whole.

        Several **senders** are one ``IN`` over the one variable and can be nothing
        else: a message has exactly one ``SENT_FROM`` edge, so "from all of these"
        has no answer, and the pattern cannot fan out either — one row per message
        whatever the pick. Several **recipients** are :func:`_addressed`'s
        business, and the two answers it *can* give are why the form has a switch
        over that field and none over this one.

    The **file filter** fans out the same way the recipients' *any* reading does
        — a message carrying three PDFs is three pattern rows — which the callers'
        ``DISTINCT`` and ``count(DISTINCT m.id)`` already take care of, and which
        is also why it is an ``IN`` over one variable rather than a traversal per
        type: several content types can only mean *any*, since one file has one
        type. It is a plain ``MATCH`` and not an ``OPTIONAL`` one, because a
        message with no matching file is a message the filter is there to exclude.

        The date bounds are **pre-encoded naive ISO strings**, compared
        lexicographically, because that is what the graph holds:
        ``sent_at`` is stored as ``isoformat()`` of whatever the ``Date`` header
        carried, wall-clock plus its own UTC offset. A string comparison reads
        wall-clock order and is blind to the offsets — exactly the margin the
        listing's ``ORDER BY sent_at`` already accepts, so a range boundary can
        misplace a message by at most its offset difference. The bounds stay
        naive (offset stripped, wall-clock kept) so a picked date means the same
        wall-clock day the rows show; against an offset-carrying stored value the
        lower bound is inclusive and the upper one excludes the exact boundary
        second, both by the string suffix. A message without a date fails both
        comparisons and drops out of any dated search — asked for a window, the
        honest answer holds only messages known to be in it.
    """
    if listing and not filters.sent_by:
        statement = statement.traverse(MESSAGE.sender, to=SENDER, optional=True)
        if filters.addressed or filters.attached or filters.account_id.strip():
            statement = statement.with_(MESSAGE, SENDER)
    if filters.sent_by:
        statement = statement.traverse(MESSAGE.sender, to=SENDER, optional=False)
        statement = statement.where(
            SENDER.id.in_([_address_key(one) for one in filters.sent_by])
        )
    statement = _addressed(statement, filters)
    if filters.account_id.strip():
        statement = statement.traverse(MESSAGE.archived_from, to=ACCOUNT)
        statement = statement.where(ACCOUNT.id == filters.account_id.strip())
    if filters.sent_from is not None:
        statement = statement.where(MESSAGE.sent_at >= _naive_iso(filters.sent_from))
    if filters.sent_until is not None:
        statement = statement.where(MESSAGE.sent_at <= _naive_iso(filters.sent_until))
    if filters.has_attachments is not None:
        statement = statement.where(MESSAGE.has_attachments == filters.has_attachments)
    if filters.attached:
        statement = statement.traverse(
            MESSAGE.attachments, to=ATTACHMENT, edge=ATTACHED, optional=False
        )
        statement = _carried(statement).where(_attached_file(filters))
    return statement


def _attached_file(filters: SearchFilters) -> Any:
    """The file pick as one predicate: this type, **or** this exact name.

    Two halves because a content type can be a shrug — see
    :attr:`~mailarc_core.archive.search.SearchFilters.attachment_names`. ORed
    and not ANDed: they are one pick expanded into the two things the archive
    stores about a file, and a message matching either is a message carrying
    something the person asked for.

    Either half alone is a complete filter, which is why each is added only
    when it holds something: an empty ``IN`` matches nothing in Cypher, and
    ORing one in would silently narrow the half that was set.
    """
    by_type = ATTACHMENT.content_type.in_(list(filters.attached_types))
    by_name = ATTACHED.filename.in_(list(filters.attached_names))
    if not filters.attached_names:
        return by_type
    if not filters.attached_types:
        return by_name
    return by_type | by_name


def _addressed(statement: Any, filters: SearchFilters) -> Any:
    """The recipient filter, in whichever of its two readings was asked for.

    **Any** is one traversal and an ``IN``: the message reached at least one of
    the picked addresses. It fans out — a message to two of them is two pattern
    rows — which is already what the callers' ``DISTINCT`` and
    ``count(DISTINCT m.id)`` are there for.

    **All** is one traversal *per address*, each with its own variable and its
    own equality, so the pattern says what it means: ``(m)-->(r0)`` and
    ``(m)-->(r1)`` can only both bind when the message reached both. There is
    no single-pattern spelling of that — an ``IN`` over one variable is "any"
    however many values it holds — and the alternative, collecting the
    recipients and counting the overlap, needs an aggregation stage this
    statement cannot carry through its page cut.

    Both walk ``SENT_TO`` and ``COPIED_TO`` and neither walks
    ``BLIND_COPIED_TO``: Bcc is what the sender hid, and a search field should
    not un-hide it. Empty picks are dropped by
    :attr:`~mailarc_core.archive.search.SearchFilters.addressed` rather than
    here, so "no recipient filter" is one question with one answer.
    """
    keys = [_address_key(one) for one in filters.addressed]
    if not keys:
        return statement
    if not filters.recipients_all:
        statement = statement.traverse(
            MESSAGE.recipients, to=RECIPIENT, types=_RECIPIENT_TYPES, optional=False
        )
        return statement.where(RECIPIENT.id.in_(keys))
    for position, key in enumerate(keys):
        target = alias(Address, f"r{position}")
        statement = statement.traverse(
            MESSAGE.recipients, to=target, types=_RECIPIENT_TYPES, optional=False
        )
        statement = statement.where(target.id == key)
    return statement


def _naive_iso(moment: datetime) -> str:
    """A range bound the stored ``sent_at`` strings compare against.

    Offset stripped, wall-clock kept — converting to UTC first would shift
    the wall-clock and make "from March 6th" mean the 5th for anyone east of
    Greenwich. See :func:`_filtered` for why the comparison is lexicographic
    at all.
    """
    return moment.replace(tzinfo=None).isoformat()


def _newest_first(
    members: list[tuple[Any, str]],
) -> list[tuple[Any, str]]:
    """One conversation's members in the order the listing shows them.

    Newest first with the id as the tiebreak — :meth:`ThreadRepository.find_members`'s
    ordering, restated here because that one sorts in Cypher and this one has
    several conversations in one answer. A member the graph has no date for
    sorts last rather than raising: ``sent_at`` is whatever the header carried.
    """
    dated = [one for one in members if one[0] is not None]
    undated = [one for one in members if one[0] is None]
    dated.sort(key=lambda one: (one[0], one[1]), reverse=True)
    undated.sort(key=lambda one: one[1])
    return [*dated, *undated]
