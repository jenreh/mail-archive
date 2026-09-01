"""Taking one mailbox's copies back out of the graph, and nothing else's.

The writer's undo. Clearing an account has to leave the archive in the state it
was in before that mailbox was ever imported, so that importing it again is a
first import rather than a resume — and it has to do that without touching mail
that reached the archive by another route.

**The same mail in two mailboxes is one node with two edges.** That is
:class:`~mailarc_core.archive.model.ArchiveSource`'s design and it is what
makes this module more than a ``DETACH DELETE``: a message archived from the
cleared account *and* from another one must survive with its other copy intact,
and only the ``ARCHIVED_FROM`` edge to the cleared account may go. So every
page is classified before anything is deleted — exclusive messages lose their
node, shared ones lose one edge — and the two never share a statement.

What is deliberately left standing
----------------------------------

``Address``, ``Thread``, ``Label`` and ``Attachment`` nodes stay where they are.
``DETACH DELETE`` takes the edges to them and leaves the nodes; a re-import
``MERGE``\\ s the same keys and links them up again, so an orphan costs a row in
the store and nothing else. Deleting them would cost something real: an
``Address`` carries ``remote_trusted``, the one property on a ground-truth node
that a *human* wrote, and an ``Attachment`` is content-addressed and shared with
every other message that ever carried the same file.

**Blobs stay too, and this module never opens the store.** The original bytes
are content-addressed and write-once (§6b): the same message imported again
hashes to the same digest and finds its blob already there. Deleting one would
mean proving no other message references those bytes — across accounts, for
attachments as well as bodies — and a wrong answer is unrecoverable, where a
kept blob costs disk.

The derived layer is not this module's business either. ``Group``, ``Topic`` and
``Template`` are disposable by construction (§5.2): their edges to a deleted
message go with it, and the next ``rebuild-derived`` recomputes them.

Synchronous, like the writer and the reader: every runic driver blocks, so an
async caller reaches this from :func:`asyncio.to_thread`.
"""

import logging
import re
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict
from runic.ogm import QueryBuilder, Session, alias, count, param, select

from mailarc_core.archive.model import Account, ArchivedFrom, Message

logger = logging.getLogger(__name__)

MESSAGE = alias(Message, "m")
"""The message under consideration — the same ``m`` every statement below binds."""

ACCOUNT = alias(Account, "a")
"""The mailbox being cleared, or in one statement the mailbox that is not it."""

COPY = alias(ArchivedFrom, "r")
"""The ``ARCHIVED_FROM`` edge, bound to a handle so the variable that is carried
through the ``WITH`` and the variable that is deleted cannot drift apart."""

PAGE_SIZE = 2000
"""Message ids read per round trip, matching the derived reader's page.

Big enough that a large mailbox is a few hundred round trips; small enough that
the id list bound into the next two statements stays a parameter and not a
payload.
"""

DELETE_BATCH = 10_000
"""Edges one copy-deletion removes per round trip.

FalkorDB has no ``CALL … IN TRANSACTIONS``, so an unbounded delete over a large
archive is one long stall on a store the UI is reading at the same time — the
same argument, and the same shape, as the derived rebuild's batching.
"""

type ProgressHook = Callable[[int], None]
"""Told the running message count after each page; called from a worker thread."""


class PurgeCounts(BaseModel):
    """What one clear-out did, in the two numbers that mean different things."""

    model_config = ConfigDict(frozen=True)

    messages: int = 0
    """Message nodes deleted — copies this account was the only holder of."""

    copies: int = 0
    """``ARCHIVED_FROM`` edges dropped off messages another account also holds.

    Those messages are still in the archive and still readable; they simply no
    longer say they came from here. Non-zero is the interesting case and worth
    reporting to whoever asked for the clear-out.
    """


def _page_ids() -> QueryBuilder[Message]:
    """A page of ids archived from this account, in id order, after a cursor.

    **A cursor and not an offset**, for the reason
    :data:`~mailarc_analytics.queries.statements.reads.MESSAGE_PROPERTIES`
    spells out and the range index on ``Message.id`` pays for: a graph store
    reaches row twenty thousand only by matching and sorting the twenty
    thousand before it. Carrying the last id forward turns each page into a
    seek.

    The cursor is what makes the loop terminate over a *shared* message too.
    Those keep their node and their edge until the very end, so a page that
    re-read from the start would hand them back for ever; ``m.id > $after``
    leaves them behind. An id-less node — which the writer does not produce but
    an older graph can hold — is skipped by the same filter, exactly as the
    listing skips it.

    Ordered on the projected column rather than on ``m.id``: after a
    ``DISTINCT`` projection ``m`` is out of scope, which is the seam the
    full-text search hit first.
    """
    return (
        select(MESSAGE)
        .where(MESSAGE.id.is_not_null() & (MESSAGE.id > param("after")))
        .traverse(MESSAGE.archived_from, to=ACCOUNT)
        .where(ACCOUNT.id == param("account"))
        .project(MESSAGE.id)
        .distinct()
        .order_by("id")
        .limit(param("batch"))
    )


def _shared_ids() -> QueryBuilder[Message]:
    """Which of these ids some *other* account also archived.

    The whole safety property of this module in one statement. What it returns
    keeps its node; what it does not is deleted.
    """
    return (
        select(MESSAGE)
        .where(MESSAGE.id.in_(param("ids")))
        .traverse(MESSAGE.archived_from, to=ACCOUNT)
        .where(ACCOUNT.id != param("account"))
        .project(MESSAGE.id)
        .distinct()
    )


def _delete_messages() -> QueryBuilder[Message]:
    """Delete these messages outright, edges and all.

    ``DETACH DELETE`` because a ``Message`` is the only node in the pattern:
    it takes the message and every edge incident to it — ``SENT_FROM``,
    ``SENT_TO``, ``LABELED``, ``HAS_ATTACHMENT``, its own ``ARCHIVED_FROM``,
    and whatever the derived layer hung on it — and leaves every node those
    edges pointed at.

    Every id here has been proved exclusive to the account being cleared, and
    the statement is narrowed by that id list alone: there is no traversal in
    it, so no predicate can be misplaced.
    """
    return (
        select(MESSAGE)
        .where(MESSAGE.id.in_(param("ids")))
        .delete(detach=True)
        .returning(count("m").as_("removed"))
    )


def _delete_copies() -> QueryBuilder[Account]:
    """Drop a batch of this account's remaining ``ARCHIVED_FROM`` edges.

    **Rooted at the account, and that is not a stylistic choice.** runic emits
    a predicate that names a traversed variable *after* the whole pipeline — so
    written from the message end, ``WHERE a.id = $account`` compiles to a
    clause standing behind the ``DELETE`` it was meant to narrow, and the
    statement would drop every account's copy of the message. Rooted at the
    account the predicate names the root, lands directly after the root
    ``MATCH``, and the account-side :attr:`~mailarc_core.archive.model.Account.copies`
    relation exists to make that rooting expressible at all. The compiled shape
    is checked against :data:`_COPY_DELETE` at import for exactly this reason.

    It carries no id filter, so it is only ever run once the page loop has
    finished: by then every message this account was the sole holder of is
    gone, taking its edge with it, and what is left is precisely the shared
    copies. ``DELETE r`` and never ``DETACH DELETE`` — detaching here would
    take the account node and the messages of every *other* mailbox with it.
    """
    return (
        select(ACCOUNT)
        .where(ACCOUNT.id == param("account"))
        .traverse(ACCOUNT.copies, to=MESSAGE, edge=COPY)
        .with_(COPY, limit=param("batch"))
        .delete(COPY)
        .returning(count("r").as_("removed"))
    )


_MESSAGE_DELETE = re.compile(
    r"MATCH \(m:Message\) "
    r"WHERE m\.id IN \$ids "
    r"DETACH DELETE m "
    r"RETURN count\(m\) AS removed"
)
"""The only shape the message deletion may have.

Deliberately exact, and read character by character, because this is the one
statement in the archive that destroys ground truth. What the shape pins is
that the deletion is narrowed by ``$ids`` and by nothing else — an edit that
dropped the ``WHERE``, or that widened the pattern to a traversal whose
predicate runic would then misplace, fails at import rather than after the
first page has gone.

The layout is normalised first (see :func:`_normalised`), so reformatting is
not mistaken for tampering. Borrowed wholesale from
:mod:`mailarc_analytics.derived.rebuild`, which made the same argument about
statements that could only delete *derived* nodes.
"""

_COPY_DELETE = re.compile(
    r"MATCH \(a:Account\) "
    r"WHERE a\.id = \$account "
    r"MATCH \(a\)<-\[r:ARCHIVED_FROM\]-\(m:Message\) "
    r"WITH r LIMIT \$batch "
    r"DELETE r "
    r"RETURN count\(r\) AS removed"
)
"""The only shape the copy deletion may have: ``DELETE`` on a relationship
variable, with the account predicate standing **before** the delete.

Both halves are the point. ``DELETE r`` rather than ``DETACH DELETE`` keeps
both endpoints, and ``WHERE a.id = $account`` before the ``DELETE`` is what
confines it to one mailbox — the placement runic gets wrong from the message
end, and the reason this statement is rooted where it is.
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


def _verified(
    statement: QueryBuilder[Message] | QueryBuilder[Account], shape: re.Pattern[str]
) -> None:
    """Raise at import unless *statement* compiles to exactly *shape*.

    Matched against the emitted Cypher rather than against a Python object: a
    statement is a query builder now, and what has to be checked is the text
    the store will run. ``build()`` suffices — a delete carries no
    dialect-supplied function — and this module refuses to import over anything
    else.
    """
    cypher = statement.build()[0]
    if not shape.fullmatch(_normalised(cypher)):
        raise ValueError(
            f"Refusing a delete statement of an unknown shape: {cypher!r} "
            f"does not match {shape.pattern!r}"
        )


_verified(_delete_messages(), _MESSAGE_DELETE)
_verified(_delete_copies(), _COPY_DELETE)


def purge_account(
    session: Session,
    account_id: str,
    *,
    page_size: int = PAGE_SIZE,
    delete_batch: int = DELETE_BATCH,
    on_progress: ProgressHook | None = None,
) -> PurgeCounts:
    """Take every copy this account holds out of the graph. Returns what it did.

    *account_id* is the ``Account`` node's key — the SQLite row id as a string,
    the way :class:`~mailarc_core.archive.model.ArchiveSource` spells it.

    Two passes, in an order that cannot orphan a message. The page loop deletes
    the exclusive messages and remembers the shared ones; only once it has run
    to the end are the account's remaining edges — which are then exactly those
    shared ones — dropped. Reversed, an interrupted run would leave messages
    behind with no account to find them by, and a second attempt could not
    reach them.

    Re-runnable, and a second call over an already-cleared account writes
    nothing: the first page comes back empty and there is no edge left to drop.

    Not atomic, and it does not pretend to be — FalkorDB has no
    multi-statement transaction. A run interrupted halfway has cleared part of
    the mailbox, which is why it is written to be run again rather than
    rolled back. The caller is expected to have made sure no import is running
    against this account; a page read while one writes would miss what the
    import adds after it.
    """
    page = _page_ids()
    shared_query = _shared_ids()
    delete = _delete_messages()

    deleted = 0
    shared: set[str] = set()
    after = ""

    while True:
        ids = [
            str(row["id"])
            for row in session.all_rows(
                page, {"after": after, "account": account_id, "batch": page_size}
            )
        ]
        if not ids:
            break
        after = ids[-1]

        elsewhere = {
            str(row["id"])
            for row in session.all_rows(
                shared_query, {"ids": ids, "account": account_id}
            )
        }
        shared |= elsewhere

        exclusive = [one for one in ids if one not in elsewhere]
        if exclusive:
            rows = session.all_rows(delete, {"ids": exclusive})
            deleted += int(rows[0].get("removed") or 0) if rows else 0
        if on_progress is not None:
            on_progress(deleted)

    copies = _drop_copies(session, account_id, delete_batch) if shared else 0
    if copies != len(shared):
        # Not fatal — the edges are gone either way — but it means the graph
        # held a copy the page loop never classified, which is what a concurrent
        # import would look like. Worth a line in the log, never a silent pass.
        logger.warning(
            "Cleared %d copies for account %s where %d were expected",
            copies,
            account_id,
            len(shared),
        )
    logger.info(
        "Cleared account %s: %d messages deleted, %d copies dropped",
        account_id,
        deleted,
        copies,
    )
    return PurgeCounts(messages=deleted, copies=copies)


def _drop_copies(session: Session, account_id: str, batch: int) -> int:
    """Loop the copy deletion until it removes nothing, and total what it did."""
    statement = _delete_copies()
    removed = 0
    while True:
        rows = session.all_rows(statement, {"account": account_id, "batch": batch})
        gone = int(rows[0].get("removed") or 0) if rows else 0
        if gone == 0:
            return removed
        removed += gone
