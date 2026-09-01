"""Clearing one mailbox out of both stores, in the order that stays recoverable.

The inverse of :class:`~mailarc_sync.engine.engine.ImportEngine`, and it lives
beside it for the reason the engine does: an import writes a graph *and* three
relational ledgers, so undoing one has to reach both stores, and this package is
the only place in the repository allowed to know both. ``mailarc_core.archive``
may not import ``mailarc_core.database``, and neither store's half is complete
on its own — a graph emptied with the ledgers left standing re-imports nothing
at all, because every listing batch is filtered through
``mail_archived_messages`` before a single message is fetched.

Order, and why it is not the obvious one
----------------------------------------

**The graph goes first, the ledgers after.** Interrupted between the two, the
mailbox is a mailbox whose messages are gone and whose ledger still claims them
— which a second run fixes, because the ledger is what the *next* clear-out
reads nothing from and the graph pass is re-runnable by construction. The
reverse order fails differently and worse: ledgers dropped first, then a crash,
leaves messages in the archive that the account can never re-import (their
provider ids come back as unseen, the writer finds the canonical id already
there, and the counts silently disagree for ever).

**The account row itself is never touched.** Clearing is not deleting: the
mailbox keeps its id, its name and the credential that opens it, because the
whole point is to import it again. The ``Account`` node in the graph stays for
the same reason — it is the key the next import's provenance edges hang off,
and re-creating it is a ``MERGE`` either way.

**Nothing is cleared while a job is open against the mailbox.** An import
running or merely queued would write into the middle of this, leaving a graph
and a ledger that disagree. There is no lock to take, so the check is a read and
the caller is told to wait.

Async on the outside and blocking within: the graph half is synchronous like
every other runic caller in this repository and is reached through
:func:`asyncio.to_thread`, exactly as the ``derive`` job reaches the rebuild.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager

from runic.ogm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from mailarc_core.archive import PurgeCounts, purge_account
from mailarc_core.database.repositories import (
    ArchivedMessageRepository,
    FailedMessageRepository,
    MailAccountRepository,
    SyncCheckpointRepository,
    SyncJobRepository,
)
from mailarc_sync.erase.model import AccountBusy, EraseCounts

logger = logging.getLogger(__name__)

type GraphSessionFactory = Callable[[], AbstractContextManager[Session]]
"""Opens a runic session. Blocking, so this module only calls it in a thread."""

type DatabaseSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
"""Opens a relational session that commits when its block leaves cleanly."""

type ProgressHook = Callable[[int], None]
"""Told the running count of deleted messages; called from the worker thread."""


class AccountEraser:
    """Empties one mailbox's import without deleting the mailbox.

    Both session factories are handed in, the way the engine's are: the
    composition root is the only module that knows how a graph and a database
    are opened, and taking them as arguments is what lets a test run the real
    thing against a temporary pair.
    """

    def __init__(
        self,
        *,
        graph_session: GraphSessionFactory,
        database_session: DatabaseSessionFactory,
    ) -> None:
        self._graph_session = graph_session
        self._database_session = database_session
        self._accounts = MailAccountRepository()
        self._jobs = SyncJobRepository()
        self._archived = ArchivedMessageRepository()
        self._failed = FailedMessageRepository()
        self._checkpoints = SyncCheckpointRepository()

    async def erase(
        self, account_id: int, *, on_progress: ProgressHook | None = None
    ) -> EraseCounts:
        """Clear this mailbox's import. Returns what came out of each store.

        Raises :class:`~mailarc_sync.erase.model.AccountBusy` when a job is
        running or queued against the account, and :class:`LookupError` when
        there is no such account — a clear-out addressed at a row that is gone
        would otherwise report a confident zero.
        """
        await self._require_idle(account_id)
        purged = await asyncio.to_thread(self._purge, str(account_id), on_progress)
        rows = await self._forget(account_id)
        counts = EraseCounts.of(purged, **rows)
        logger.info(
            "Cleared account %d: %d messages, %d shared copies, %d ledger rows",
            account_id,
            counts.messages,
            counts.copies,
            counts.archived_rows,
        )
        return counts

    def _purge(self, account_key: str, on_progress: ProgressHook | None) -> PurgeCounts:
        """The graph half, in a thread of its own because runic blocks."""
        with self._graph_session() as session:
            return purge_account(session, account_key, on_progress=on_progress)

    async def _require_idle(self, account_id: int) -> None:
        """Refuse the clear-out unless the mailbox is standing still.

        Both checks in one session, and the account read first: "no such
        account" and "that account is busy" are different sentences, and a
        caller that got the second for a deleted row would go looking in the
        wrong place.
        """
        async with self._database_session() as session:
            if await self._accounts.find_by_id(session, account_id) is None:
                raise LookupError(f"account {account_id} is gone")
            open_jobs = await self._jobs.find_open_for_account(session, account_id)
        if not open_jobs:
            return
        raise AccountBusy(
            f"An import is still {open_jobs[0].state} for this mailbox — "
            "let it finish or cancel it, then clear the mailbox"
        )

    async def _forget(self, account_id: int) -> dict[str, int]:
        """The relational half: the three ledgers, in one transaction.

        One session for all three, because they are one decision. Two of them
        are what the import reads to decide what it has already done, and a
        clear-out that dropped one and not the other would leave the mailbox
        importable in a way that skips exactly the wrong messages.
        """
        async with self._database_session() as session:
            archived = await self._archived.delete_for_account(session, account_id)
            checkpoints = await self._checkpoints.delete_for_account(
                session, account_id
            )
            failures = await self._failed.delete_for_account(session, account_id)
            account = await self._accounts.find_by_id(session, account_id)
            if account is not None:
                # The mailbox has never been synced again — saying otherwise
                # would put a date on a page that is now showing nothing.
                account.last_sync_at = None
                account.last_error = None
        return {
            "archived_rows": archived,
            "checkpoints": checkpoints,
            "failures": failures,
        }
