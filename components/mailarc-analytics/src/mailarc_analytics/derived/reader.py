"""The one place the derived layer reads the ground truth.

Every analysis works from :class:`~mailarc_analytics.derived.model.MessageFacts`
and nothing else, so there is exactly one module that knows the shape of a
``Message`` node — and exactly one place to look when a rebuild disagrees with
the archive. The three analyses below it are pure functions over value objects
and can be tested without a graph at all, which is the whole point of putting
the reading here.

**The SimHash conversion happens here and only here.** The writer stores
``to_signed_64(simhash)`` because every Cypher backend's integer is signed
64-bit, and the sign bit is set on roughly half of all real messages. Reading
that value back as it stands and handing it on is the one bug this phase is
most likely to ship:
:func:`~mailarc_core.mail.parsing.hamming_distance` is
``(left ^ right).bit_count()``, and ``int.bit_count()`` counts the ones of the
*absolute* value, so a mixed-sign comparison silently answers 62 where the
truth is 2 — measured, wrong on 47.6 % of all pairs. Rendering is no better:
``f"{value:016x}"`` on a stored fingerprint emits a leading minus and two runs
disagreeing about a minus sign are not a key. So
:func:`~mailarc_core.archive.model.to_unsigned_64` runs once, at this boundary,
and ``MessageFacts.simhash`` is unsigned by construction. Nothing downstream
has to remember.

Two reads, deliberately not one. :func:`read_facts` returns everything the
clustering needs and no message text; :func:`read_bodies` fetches the cleaned
bodies of named messages afterwards. ``body_clean`` is uncapped, so reading it
for a hundred thousand messages would put hundreds of megabytes of Python
strings beside a FalkorDB that runs in the same process tree — for the sake of
a few hundred texts that end up in a template.

Four more reads follow the same rule for the same reason. :func:`read_replies`
and :func:`read_signals` are each a **separate paged read** rather than more
columns on the two the facts come from: ``MESSAGE_RELATIONS`` already carries
five optional expansions that cross-multiply per message, and the reply chain
and the label set would multiply into the same product.
:func:`read_texts` is the keyword stage's late read — :func:`read_bodies` with
a ceiling, because counting terms needs the first two thousand characters and
not the whole letter. :func:`read_tagged` reads the **annotation layer**, the
one thing in this file that no rebuild may write: a suggestion is only allowed
to name a message that is not tagged yet, and the score is a share of the
members that are.

Every statement comes from :mod:`mailarc_analytics.queries.catalog` and every
one of them is now a query-builder statement, run through
:func:`~mailarc_analytics.queries.rows.rows_of` — which is
``session.all_rows(statement, params)`` for a builder and the zip for the one
raw entry this package still has. :mod:`mailarc_core.archive.repository` states
the house rule that graph reads go through the builder, and the catalogue used
to argue four exceptions to it; runic 0.5 closed all four, A1's
``[:SENT_TO|COPIED_TO]`` alternation included — it is
``traverse(types=[…])`` now. The zip is still here for one statement, and its
docstring names the condition that retires it.

Synchronous, because every runic driver blocks. An async caller wraps a call in
``asyncio.to_thread`` the way the graph status reader does.
"""

import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from runic.ogm import Session

from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.findings import MessageSignals
from mailarc_analytics.derived.model import MessageFacts
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import Statement
from mailarc_analytics.queries.rows import rows_of
from mailarc_core.archive.model import to_unsigned_64

logger = logging.getLogger(__name__)

PAGE_SIZE = 2000
"""Messages one read of the ground truth asks for at a time.

Not configuration. It trades round trips against the size of one result set,
and neither number is something a user has an opinion about — the setting that
*is* theirs is
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.max_messages`, which
says how much of the archive a rebuild may look at.
"""

BODY_BATCH = 500
"""Bodies one :data:`~mailarc_analytics.queries.catalog.MESSAGE_BODIES` call
asks for. Small, because each row carries an uncapped text."""


def read_account_addresses(session: Session) -> frozenset[str]:
    """Every address this archive imports from, lowercased.

    What "sent by me" means, and therefore what A3 is allowed to call
    automatable. The answer is only ever as good as the account list: a shared
    mailbox nobody archived, or an alias the user sends under, makes their own
    mail look received until that address is imported too.
    """
    found = {
        str(row["address"]).strip().lower()
        for row in rows_of(session, catalog.ACCOUNT_ADDRESSES)
        if row.get("address")
    }
    logger.debug("Archive owns %d addresses", len(found))
    return frozenset(found)


def count_unidentified(session: Session) -> int:
    """``Message`` nodes without a canonical id — the ones the reads step over.

    Asked separately rather than inferred from a shortfall, because the two
    reads filter those nodes out in Cypher and a caller comparing counts would
    only learn that something was missing, not what.
    """
    rows = rows_of(session, catalog.COUNT_UNIDENTIFIED)
    total = int(rows[0]["total"]) if rows else 0
    if total:
        logger.warning("Skipping %d Message nodes without a canonical id", total)
    return total


def count_messages(session: Session) -> int:
    """How many messages :func:`read_facts` would return without a ceiling.

    Asked only when
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.max_messages` is
    set, and only so that what the ceiling left out is a number rather than an
    absence. Every other omission in this package already is one — the
    unidentified nodes, the too-widely-addressed messages, the bodies with no
    fingerprint, both kinds of dropped bucket — and a rebuild capped at a
    thousand messages of a hundred thousand otherwise reported exactly what a
    rebuild of a small archive reports.

    A second statement rather than a shortfall against
    :func:`count_unidentified`: the two filters are complements, and inferring
    one population from the other would break the day a third kind of node
    turns up.
    """
    rows = rows_of(session, catalog.COUNT_MESSAGES)
    return int(rows[0]["total"]) if rows else 0


def read_facts(session: Session, config: AnalyticsConfig) -> tuple[MessageFacts, ...]:
    """Every archived message, reduced to what the three analyses read.

    Two statements joined in Python rather than one wide read: the properties
    include ``refs``, which is a list, and the relations have to be aggregated
    per message. A list in the grouping key of an aggregating ``RETURN`` is
    asking a graph store for trouble it has no reason to give, so the scalars
    come back on their own and the collected sets come back on their own, both
    ordered by the canonical id, both paged in lockstep.

    That ordering is not decoration. A ``LIMIT`` without an ``ORDER BY`` is an
    arbitrary subset in Cypher, so a capped rebuild would read different
    messages each time, cluster them differently and mint different topic ids —
    which is exactly what the idempotence contract forbids.
    """
    own = read_account_addresses(session)
    ceiling = max(0, config.max_messages)
    properties = list(_paged(session, catalog.MESSAGE_PROPERTIES, ceiling))
    relations = {
        str(row["id"]): row
        for row in _paged(session, catalog.MESSAGE_RELATIONS, ceiling)
    }
    facts = tuple(
        _facts(row, relations.get(str(row["id"]), {}), own) for row in properties
    )
    logger.info(
        "Read %d messages, %d of them sent by this archive",
        len(facts),
        sum(1 for one in facts if one.outbound),
    )
    return facts


def read_bodies(session: Session, ids: Sequence[str]) -> dict[str, str]:
    """The cleaned bodies of exactly these messages.

    A3's second read, and the reason the first one leaves the text behind. Only
    the members of an actual template need their words — for the sample a human
    recognises the text by, and for the word count the brevity factor uses —
    and that is a few hundred messages out of an archive, not all of them.

    A message whose ``body_clean`` is missing is simply absent from the answer;
    the caller falls back to what it already holds rather than to a guess.
    """
    found: dict[str, str] = {}
    for batch in _batched(ids, BODY_BATCH):
        for row in rows_of(session, catalog.MESSAGE_BODIES, {"ids": list(batch)}):
            body = row.get("body_clean")
            if body:
                found[str(row["id"])] = str(body)
    logger.debug("Read %d bodies for %d requested messages", len(found), len(ids))
    return found


def read_replies(session: Session, config: AnalyticsConfig) -> dict[str, str]:
    """Which message each reply answers — ``{reply id: parent id}``.

    Half of what makes a *conversation*, and the half a provider cannot give.
    ``Thread`` groups the copies one account holds, so the same exchange
    imported from two mailboxes is two threads; ``In-Reply-To`` is the sender's
    own statement that this message answers that one and crosses accounts.
    Union-find over both is signal 7,
    :attr:`~mailarc_analytics.derived.model.TopicSignal.CONVERSATION`.

    Takes the configuration for the ceiling, not for a threshold — and the
    ceiling counts **replies** here where it counts messages in
    :func:`read_facts`, because this statement's rows are the reply table
    rather than the archive. So a capped rebuild reads the first *n* replies in
    canonical id order, which is a different prefix from the first *n*
    messages: some of the parents it names are outside what the facts read saw.
    That is safe and is where it has to be resolved —
    :func:`~mailarc_analytics.derived.conversations.conversation_edges` takes
    the facts as well as this mapping and joins against the ids it actually
    holds, so a parent nobody read joins nothing. What the ceiling buys is the
    same thing it buys everywhere else: a bounded read on a large archive.

    A message with no parent is simply absent: the statement's match is an
    inner one, so the read is the size of the reply table and not of the
    archive.
    """
    ceiling = max(0, config.max_messages)
    found = {
        str(row["id"]): str(row["parent"])
        for row in _paged(session, catalog.MESSAGE_REPLIES, ceiling)
        if row.get("parent")
    }
    logger.debug("Read %d replies", len(found))
    return found


def read_signals(
    session: Session, config: AnalyticsConfig
) -> dict[str, MessageSignals]:
    """What each message says about its own importance, keyed by id.

    A read of its own rather than four more columns on ``MESSAGE_RELATIONS``,
    for the reason that statement's docstring gives: five optional expansions
    already cross-multiply per message, and these four would multiply into the
    same product. It also asks a different question — ``MessageFacts.addressed``
    is To *and* Cc folded together, which is what co-addressing is defined
    over, while "addressed directly" is a claim about the To line alone.

    Keyed by id and not returned as a tuple, because the scorer walks the facts
    and asks this for each of them. A message no row came back for is scored on
    its own properties: every field of
    :class:`~mailarc_analytics.derived.findings.MessageSignals` defaults to the
    absence of the signal, so a caller may use ``found.get(id)`` and a missing
    row costs a message its reasons rather than the run.
    """
    ceiling = max(0, config.max_messages)
    found = {
        str(row["id"]): _signals(row)
        for row in _paged(session, catalog.MESSAGE_SIGNALS, ceiling)
    }
    logger.debug("Read signals for %d messages", len(found))
    return found


def read_texts(session: Session, ids: Sequence[str], max_chars: int) -> dict[str, str]:
    """The subject and a capped body of exactly these messages, joined.

    The keyword stage's read, and :func:`read_bodies` with a ceiling. A3 needs
    a template member's *whole* cleaned body — for the sample a human
    recognises it by and the word count the brevity factor divides — while
    counting terms needs enough text to count in and no more. ``$max_chars``
    cuts it in the store, so a page carries what it needs rather than the whole
    letter.

    Subject and body come back as one string because a piece of work is
    usually *named* in the subject and described in the body — "Angebot
    Datenmigration" is the keyword and the body says "anbei unser Angebot" —
    and the tokeniser should see both. Nothing here decides how they are
    weighed; a term is a term wherever it was written.

    A message with neither a subject nor a body is absent from the answer
    rather than present as an empty string, so a caller counting documents
    counts the ones that had something to say.
    """
    found: dict[str, str] = {}
    for batch in _batched(ids, BODY_BATCH):
        for row in rows_of(
            session,
            catalog.MESSAGE_TEXTS,
            {"ids": list(batch), "max_chars": max_chars},
        ):
            text = " ".join(
                part for part in (row.get("subject"), row.get("body")) if part
            ).strip()
            if text:
                found[str(row["id"])] = text
    logger.debug("Read %d texts for %d requested messages", len(found), len(ids))
    return found


def read_tagged(session: Session) -> dict[str, frozenset[str]]:
    """Which messages wear which tag, right now — ``{tag id: message ids}``.

    The one read in this file that touches the **annotation layer**, and it is
    a read and never anything else: ``TAGGED`` records what a human decided and
    is written by :mod:`mailarc_core.archive.tags` alone. The suggestion pass
    needs it for both halves of its own rule — a message that already wears the
    tag is not suggested again, and the share of a group that does is the
    score.

    Not paged, and bounded by what it reads: a membership row is two ids, and
    the population is the mail somebody tagged by hand. A tag whose messages
    were all purged with an account comes back absent rather than empty, which
    is the same thing to a caller counting members.
    """
    found: dict[str, set[str]] = {}
    for row in rows_of(session, catalog.TAGGED_MEMBERSHIP):
        tag = str(row["tag_id"])
        found.setdefault(tag, set()).add(str(row["message_id"]))
    logger.debug("Read %d tags with memberships", len(found))
    return {tag: frozenset(members) for tag, members in found.items()}


def _signals(row: Mapping[str, Any]) -> MessageSignals:
    """One ``MESSAGE_SIGNALS`` row as the value the scorer reads.

    ``reply_count`` comes back as a number the driver may hand over as a float,
    the way every aggregate in this package does, so it goes through ``int``
    here rather than at each of the scorer's comparisons.
    """
    return MessageSignals(
        id=str(row["id"]),
        sent_to=_address_set(row.get("to")),
        reply_count=int(row.get("reply_count") or 0),
        replied_by=_address_set(row.get("replied_by")),
        label_names=_text_set(row.get("label_names")),
        has_attachments=bool(row.get("has_attachments")),
    )


def _facts(
    properties: Mapping[str, Any],
    relations: Mapping[str, Any],
    own: frozenset[str],
) -> MessageFacts:
    """One row from each read, folded into the value the analyses work from."""
    sender = _first(relations.get("senders"))
    addressed = _address_set(relations.get("addressed"))
    blind = _address_set(relations.get("blind_copied"))
    return MessageFacts(
        id=str(properties["id"]),
        sent_at=_as_datetime(properties.get("sent_at")),
        subject_norm=str(properties.get("subject_norm") or ""),
        participant_key=str(properties.get("participant_key") or ""),
        simhash=to_unsigned_64(int(properties.get("simhash") or 0)),
        refs=_text_set(properties.get("refs")),
        thread_id=_first(relations.get("threads")),
        sender=sender or "",
        addressed=addressed,
        participants=_participants(sender, addressed, blind),
        attachments=_text_set(relations.get("attachments")),
        outbound=bool(sender) and sender in own,
    )


def _participants(
    sender: str | None, addressed: tuple[str, ...], blind: tuple[str, ...]
) -> tuple[str, ...]:
    """Everyone on the message, Bcc included — what a group is counted over.

    ``participant_key`` is a hash of the sorted set of sender, To, Cc *and*
    Bcc, so a group whose size was counted over anything narrower would
    disagree with its own key. The Bcc addresses stay out of ``addressed``
    all the same: they were written to without the others knowing, and a
    ``CO_ADDRESSED`` edge between them would materialise exactly the
    confidentiality the header exists to protect.
    """
    everyone = {*addressed, *blind}
    if sender:
        everyone.add(sender)
    return tuple(sorted(everyone))


def _paged(
    session: Session, statement: Statement, ceiling: int
) -> Iterator[dict[str, Any]]:
    """Walk an ordered statement page by page, stopping at *ceiling* if set.

    The page boundary is the last id seen, not a running offset. ``SKIP`` is
    correct and quadratic — a graph store reaches row twenty thousand by
    matching, expanding and sorting the twenty thousand before it, so every
    page pays for the whole archive again — while a cursor makes each page an
    index seek from where the last one stopped. Both statements order by the
    canonical id and nothing else, so the largest id on a page is its last row.

    A short page ends the walk: the ordering makes the boundary stable, so
    there is nothing after one that came back thin.
    """
    after = ""
    read = 0
    while True:
        limit = PAGE_SIZE if ceiling <= 0 else min(PAGE_SIZE, ceiling - read)
        if limit <= 0:
            return
        page = rows_of(session, statement, {"after": after, "limit": limit})
        yield from page
        read += len(page)
        if len(page) < limit:
            return
        after = str(page[-1]["id"])


def _batched(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """Cut a sequence into chunks of at most *size*.

    Memory, not a driver limit. The only caller binds the chunk to
    :data:`~mailarc_analytics.queries.catalog.MESSAGE_BODIES`'s ``$ids``, and
    every row that comes back carries an *uncapped* message body — so asking
    for one id per archived message would pull the text of the whole archive
    into one result set before a single body was looked at. The ids themselves
    would fit comfortably; what will not is the answer.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _first(values: Any) -> str | None:
    """The smallest entry of a collected column, or ``None`` if it is empty.

    Smallest rather than first, because ``collect`` makes no promise about
    order. Only ever asked of columns that hold at most one value — a message
    has one sender and one thread — so the choice matters exactly when the
    graph holds something it should not, and then it is at least stable.
    """
    found = _text_set(values)
    return found[0] if found else None


def _text_set(values: Any) -> tuple[str, ...]:
    """A collected column as a sorted, deduplicated tuple of non-empty strings.

    ``collect`` drops nulls on its own; the filter is for the empty string,
    which is a property that was written rather than one that is missing.
    """
    if not values:
        return ()
    return tuple(sorted({str(one) for one in values if one}))


def _as_datetime(value: Any) -> datetime | None:
    """A stored timestamp as an **aware** ``datetime``, or ``None``.

    The graph hands back the ISO-8601 string runic's mapper wrote, because a
    *projected* column goes past the converter that would have decoded it: the
    statement is a builder now and ``all_rows`` decodes a column only where a
    whole node or edge comes back under a mapped alias, which was measured
    rather than assumed. A value that does not parse costs this message its
    date and nothing else — every analysis already handles an undated message,
    and a rebuild that died over one malformed property would be worse than one
    that reports it.

    A value that parses but carries no offset is the gap between those two
    cases, and the expensive one: every ``min``, ``max``, ``sorted`` and
    subtraction over the archive's dates would raise the moment a naive value
    met an aware one, which ends the run rather than one message. So a missing
    zone is read as UTC — the same decision
    :func:`~mailarc_core.mail.parsing._sent_at` makes for a ``Date`` header
    that withholds one, kept here as well so that a node the writer did not
    produce cannot reach the analyses in a shape they have no defence against.
    """
    if isinstance(value, datetime):
        return _aware(value)
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        logger.warning("Ignoring unparseable timestamp %r", value)
        return None


def _aware(value: datetime) -> datetime:
    """The same instant, with UTC put back where a zone is missing."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _address_set(values: Any) -> tuple[str, ...]:
    """A collected column of address ids, lowercased the way the nodes are."""
    return tuple(sorted({one.strip().lower() for one in _text_set(values)}))
