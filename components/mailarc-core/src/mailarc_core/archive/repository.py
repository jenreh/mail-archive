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

from mailarc_core.archive.model import Account, Address, Label, Message
from mailarc_core.archive.search import ScoredId, SearchFilters, searchable_terms

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
"""The recipient filter's target, reached over ``SENT_TO`` **and** ``COPIED_TO``.

One alternation pattern rather than two traversals: "sent or copied to" is one
walk, and two separate patterns would double-count a message that did both.
``BLIND_COPIED_TO`` is deliberately out — Bcc is what the sender hid, and a
search field should not un-hide it as a side effect.
"""

ACCOUNT = alias(Account, "a")
"""The ``ARCHIVED_FROM`` target — which mailbox imported this copy."""

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


class AddressRepository(Repository[Address]):
    """Addresses, for the one thing a human writes back: remote-content trust."""

    def __init__(self, session: Session) -> None:
        super().__init__(session, Address)

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

    Sender and recipient match by **containment** on the normalised address —
    ``anna`` or ``@firma.de`` narrows the way a mail client's search does —
    lowercased like the node keys, so the match is case-insensitive. The
    account is an exact key: it comes from a picker, not a text field.

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
    if listing and not filters.sender.strip():
        statement = statement.traverse(MESSAGE.sender, to=SENDER, optional=True)
        if filters.recipient.strip() or filters.account_id.strip():
            statement = statement.with_(MESSAGE, SENDER)
    if filters.sender.strip():
        statement = statement.traverse(MESSAGE.sender, to=SENDER, optional=False)
        statement = statement.where(SENDER.id.contains(_address_key(filters.sender)))
    if filters.recipient.strip():
        statement = statement.traverse(
            MESSAGE.recipients, to=RECIPIENT, types=_RECIPIENT_TYPES, optional=False
        )
        statement = statement.where(
            RECIPIENT.id.contains(_address_key(filters.recipient))
        )
    if filters.account_id.strip():
        statement = statement.traverse(MESSAGE.archived_from, to=ACCOUNT)
        statement = statement.where(ACCOUNT.id == filters.account_id.strip())
    if filters.sent_from is not None:
        statement = statement.where(MESSAGE.sent_at >= _naive_iso(filters.sent_from))
    if filters.sent_until is not None:
        statement = statement.where(MESSAGE.sent_at <= _naive_iso(filters.sent_until))
    if filters.has_attachments is not None:
        statement = statement.where(MESSAGE.has_attachments == filters.has_attachments)
    return statement


def _naive_iso(moment: datetime) -> str:
    """A range bound the stored ``sent_at`` strings compare against.

    Offset stripped, wall-clock kept — converting to UTC first would shift
    the wall-clock and make "from March 6th" mean the 5th for anyone east of
    Greenwich. See :func:`_filtered` for why the comparison is lexicographic
    at all.
    """
    return moment.replace(tzinfo=None).isoformat()
