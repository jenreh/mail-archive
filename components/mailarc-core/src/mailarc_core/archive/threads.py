"""Giving every message back the conversation its headers already name.

The writer's third key rule, applied to mail that was archived before there
was one. :func:`~mailarc_core.archive.writer._thread_node` keys a ``Thread``
on the provider's own conversation id, else on the root taken out of
``References``, else on the message's own ``Message-ID`` — and that last
fallback is what gives a conversation's *first* message a thread at all. Mail
imported before it existed carries no ``IN_THREAD`` edge, so a grouped listing
shows the replies under one heading and leaves the message they answer sitting
beside them as an unrelated row.

**Nothing else repairs it.** The import engine drops every reference
``mail_archived_messages`` already knows before a single message is fetched, so
re-syncing a mailbox never rewrites an edge for mail that is already in it. The
alternative to this module is clearing the account and importing it again,
which throws away a working archive to fix a missing edge.

What it does and does not touch
-------------------------------

It writes ``Thread`` nodes and ``IN_THREAD`` edges, and nothing else. No
message is read, changed or deleted; a message that already sits in a
conversation is passed over, and one that can name no conversation — no
``Message-ID``, no provider thread id, or no mailbox to scope a key to — is
counted rather than guessed at.

**The key is the writer's, not a second copy of it.** Both call
:func:`~mailarc_core.archive.model.thread_key`, so a message repaired here
lands in exactly the node the next import would have merged it onto. What this
module cannot see is ``References``: the header is not on the node, only the
root it produced, which is why a message that *has* one already has its edge —
the writer wrote it at import — and a message that has none is keyed on itself,
which is the whole of the rule that was missing.

Synchronous, like the writer and the purge: every runic driver blocks, so an
async caller reaches this from :func:`asyncio.to_thread`.
"""

import logging
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict
from runic.ogm import QueryBuilder, Session, alias, count, param, select

from mailarc_core.archive.model import (
    Account,
    ArchivedFrom,
    Message,
    Thread,
    thread_key,
)
from mailarc_core.archive.writer import IN_THREAD

logger = logging.getLogger(__name__)

MESSAGE = alias(Message, "m")
ACCOUNT = alias(Account, "a")
THREAD = alias(Thread, "t")
COPY = alias(ArchivedFrom, "r")
"""The four variables every statement below binds, named once."""

PAGE_SIZE = 500
"""Message ids read per round trip.

Smaller than the purge's two thousand because a page is not only scanned: the
ids that come back are asked about twice more, and both of those statements
bind the page as a parameter list.
"""

type ProgressHook = Callable[[int], None]
"""Told the running count of messages looked at, after each page."""


class ThreadRepairCounts(BaseModel):
    """What one repair did, in numbers that mean four different things."""

    model_config = ConfigDict(frozen=True)

    scanned: int = 0
    """Messages looked at — the whole archive, however few needed anything."""

    linked: int = 0
    """``IN_THREAD`` edges written.

    Above :attr:`threads` by however many messages joined a conversation that
    already existed, which is the number a reader actually sees change: those
    are the roots that were sitting beside their own replies.
    """

    threads: int = 0
    """``Thread`` nodes created — one per conversation nothing had opened yet.

    Mostly conversations of one, and they draw as plain rows exactly as they
    did before. The node is what a reply arriving tomorrow is merged onto.
    """

    unkeyed: int = 0
    """Messages that can carry no conversation key, and were left alone.

    A message whose canonical id is a ``sha256:`` digest has no ``Message-ID``
    for a reply to reference, and one no ``Account`` holds has no mailbox to
    scope a thread to. Counted rather than passed over in silence: a non-zero
    number here is the difference between "nothing needed doing" and "something
    could not be done".
    """


def count_unthreaded(session: Session) -> int:
    """How many messages sit in no conversation at all.

    Two counts and a subtraction rather than a scan for the absence of an
    edge: a pattern predicate over every message is a full expansion, and this
    number is read to *offer* the repair — on a page a person opens, before
    they have asked for anything.

    Clamped at zero. The two reads are made one after the other against a
    graph an import may be writing into, and a panel must never offer to
    repair minus one message.
    """
    total = _total(session, _all_messages())
    threaded = _total(session, _threaded_messages())
    return max(0, total - threaded)


def repair_threads(
    session: Session,
    *,
    page_size: int = PAGE_SIZE,
    on_progress: ProgressHook | None = None,
) -> ThreadRepairCounts:
    """Link every message that sits in no conversation. Returns what it did.

    One page of ids at a time, ordered and carried forward by a cursor — the
    purge's walk, for the reason it gives: a graph store reaches row twenty
    thousand only by matching and sorting the twenty thousand before it, and
    the range index on ``Message.id`` turns each page into a seek.

    Re-runnable, and a second run over a repaired archive writes nothing: the
    first thing each page asks is which of its messages already have a thread,
    and by then all of them do. Not atomic either — FalkorDB has no
    multi-statement transaction — which is why it is written to be run again
    rather than rolled back. A run interrupted halfway has repaired the pages
    it reached, and the next run starts from the beginning and skips them.

    The caller is expected to have made sure no import is running: a page read
    while one writes simply misses what the import adds after it, and the next
    run picks that up.
    """
    page = _page_ids()
    scanned = 0
    linked = 0
    created = 0
    unkeyed = 0
    after = ""

    while True:
        ids = [
            str(row["id"])
            for row in session.all_rows(page, {"after": after, "batch": page_size})
        ]
        if not ids:
            break
        after = ids[-1]
        scanned += len(ids)

        filed = _threaded(session, ids)
        missing = [one for one in ids if one not in filed]
        if missing:
            page_counts = _link(session, missing)
            linked += page_counts.linked
            created += page_counts.threads
            unkeyed += page_counts.unkeyed
        if on_progress is not None:
            on_progress(scanned)

    logger.info(
        "Repaired conversations over %d messages: %d edges written, "
        "%d threads created, %d messages could not be keyed",
        scanned,
        linked,
        created,
        unkeyed,
    )
    return ThreadRepairCounts(
        scanned=scanned, linked=linked, threads=created, unkeyed=unkeyed
    )


def _link(session: Session, ids: list[str]) -> ThreadRepairCounts:
    """File one page's threadless messages, creating what they need first.

    Nodes before edges, and flushed in between, for the reason the writer
    states: an edge is a ``MERGE`` over two ``MATCH`` clauses, so relating a
    node that has not reached the graph yet writes nothing and says nothing.

    **The message end of each edge is a handle, not a loaded node.**
    ``relate`` compiles to ``MATCH`` on both primary keys and a ``MERGE`` of
    the relationship — it writes no node property from either end — so
    ``Message(id=…)`` is all it needs, and reading a page of real nodes back
    would drag 64 KB of indexed body text across the wire per message to write
    one edge. The ids come out of the graph a statement earlier, so the match
    cannot miss.
    """
    wanted, subjects = _keys_of(session, ids)
    unkeyed = len([one for one in ids if one not in wanted])
    if not wanted:
        return ThreadRepairCounts(unkeyed=unkeyed)

    keys = sorted({key for found in wanted.values() for key in found})
    known = _existing(session, keys)
    threads = {
        one: known.get(one) or _new_thread(session, one, subjects) for one in keys
    }
    session.flush()

    linked = 0
    for message_id, found in wanted.items():
        for key in sorted(found):
            session.relate(Message(id=message_id), IN_THREAD, threads[key])
            linked += 1
    session.flush()
    logger.debug(
        "Linked %d of %d messages into %d conversations",
        len(wanted),
        len(ids),
        len(keys),
    )
    return ThreadRepairCounts(
        linked=linked, threads=len(keys) - len(known), unkeyed=unkeyed
    )


def _keys_of(
    session: Session, ids: list[str]
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Which conversation each of these messages belongs in, and what to call it.

    One row per ``(message, mailbox)`` pair, because the same mail archived
    from two mailboxes is one node with two provenance edges and belongs in
    each mailbox's own conversation — a ``Thread`` is keyed per account.

    The subject is carried along only to name a node that has to be created;
    the writer names one the same way, after the message that opened it.
    """
    rows = session.all_rows(_provenance(), {"ids": ids})
    wanted: dict[str, set[str]] = {}
    subjects: dict[str, str] = {}
    for row in rows:
        message_id = str(row.get("message_id") or "")
        account_id = str(row.get("account_id") or "")
        named = str(row.get("provider_thread_id") or row.get("rfc_message_id") or "")
        if not message_id or not account_id or not named:
            continue
        key = thread_key(account_id, named)
        wanted.setdefault(message_id, set()).add(key)
        subjects.setdefault(key, str(row.get("subject") or ""))
    return wanted, subjects


def _new_thread(session: Session, key: str, subjects: dict[str, str]) -> Thread:
    """Open a conversation nobody had opened, named after the message in it."""
    node = Thread(id=key, subject=subjects.get(key) or None)
    session.add(node)
    return node


def _existing(session: Session, keys: list[str]) -> dict[str, Thread]:
    """The conversations of these keys that the graph already holds.

    Read as ids and rebuilt as handles rather than loaded as entities: what a
    ``MERGE`` of an edge needs from either end is a primary key, and a
    ``Thread`` read back would carry a subject this module has no business
    rewriting.
    """
    rows = session.all_rows(_threads_by_key(), {"keys": keys})
    found = [str(row.get("id") or "") for row in rows]
    return {one: Thread(id=one) for one in found if one}


def _threaded(session: Session, ids: list[str]) -> set[str]:
    """Which of these messages already sit in a conversation."""
    rows = session.all_rows(_messages_with_thread(), {"ids": ids})
    return {str(row.get("message_id") or "") for row in rows}


def _total(session: Session, statement: QueryBuilder[Message]) -> int:
    rows = session.all_rows(statement)
    return int(rows[0].get("total") or 0) if rows else 0


def _page_ids() -> QueryBuilder[Message]:
    """A page of message ids, in id order, after a cursor.

    The purge's ``_page_ids`` without its account traversal: this repair is
    about the whole archive, and an id-less node — which the writer does not
    produce but an older graph can hold — is skipped by the same filter the
    listing skips it with.
    """
    return (
        select(MESSAGE)
        .where(MESSAGE.id.is_not_null() & (MESSAGE.id > param("after")))
        .project(MESSAGE.id)
        .distinct()
        .order_by("id")
        .limit(param("batch"))
    )


def _messages_with_thread() -> QueryBuilder[Message]:
    """Which of ``$ids`` carry an ``IN_THREAD`` edge — a required traversal."""
    return (
        select(MESSAGE)
        .where(MESSAGE.id.in_(param("ids")))
        .traverse(MESSAGE.thread, to=THREAD, optional=False)
        .project(MESSAGE.id.as_("message_id"))
        .distinct()
    )


def _provenance() -> QueryBuilder[Message]:
    """Everything a thread key is made of, one row per mailbox that holds it."""
    return (
        select(MESSAGE)
        .where(MESSAGE.id.in_(param("ids")))
        .traverse(MESSAGE.archived_from, to=ACCOUNT, edge=COPY, optional=False)
        .project(
            MESSAGE.id.as_("message_id"),
            MESSAGE.rfc_message_id.as_("rfc_message_id"),
            MESSAGE.subject.as_("subject"),
            ACCOUNT.id.as_("account_id"),
            COPY.provider_thread_id.as_("provider_thread_id"),
        )
    )


def _threads_by_key() -> QueryBuilder[Thread]:
    """Which of ``$keys`` name a conversation the graph already holds."""
    return (
        select(THREAD).where(THREAD.id.in_(param("keys"))).project(THREAD.id).distinct()
    )


def _all_messages() -> QueryBuilder[Message]:
    return (
        select(MESSAGE)
        .where(MESSAGE.id.is_not_null())
        .project(count(MESSAGE.id, distinct=True).as_("total"))
    )


def _threaded_messages() -> QueryBuilder[Message]:
    return (
        select(MESSAGE)
        .where(MESSAGE.id.is_not_null())
        .traverse(MESSAGE.thread, to=THREAD, optional=False)
        .project(count(MESSAGE.id, distinct=True).as_("total"))
    )


def _normalised(cypher: str) -> str:
    """Return *cypher* as one line, with the compiler's backticks removed.

    The purge's helper, and the tests next door use it for the purge's own
    reason: runic compiles over several lines and backtick-quotes every
    identifier, so a statement is told apart by what it says rather than by
    how it was laid out.
    """
    return " ".join(cypher.replace("`", "").split())
