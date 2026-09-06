"""The recurring trigger: when a mailbox should be looked at again.

Beside :mod:`mailarc_sync.jobs.queue` rather than inside it. The queue is
deliberately not a scheduler (§7.2) — it decides who may run a job and what
became of it, never that one should exist. This module is the other half of
that sentence and knows only the *when*: it waits out an interval, asks
:class:`~mailarc_sync.jobs.sweep.MailboxSweep` for a round of deltas, and goes
back to waiting. Nothing here opens a mailbox, and nothing here can fail a job,
because at the moment it runs there is no job yet.

*Which* mailbox owes a run is :mod:`mailarc_sync.jobs.sweep`'s question and was
moved there when the rail grew a Sync button: pressing it asks exactly the same
four things of an account that a timer does, and the two answers must not be
allowed to drift. What is left here is the loop.

appkit's ``APScheduler`` is the alternative and §3.3 measured it against this,
with three findings each sufficient on its own. It is **not installed**:
``from appkit_commons.scheduler import APScheduler`` yields literally ``None``
in this venv, because that import sits in a ``try/except ImportError``. It is
**PostgreSQL-bound**: ``_configure_scheduler`` builds a ``PsycopgEventBroker``
from the database url, and on ``sqlite+aiosqlite://`` the constructor throws
into an ``except Exception`` that falls back to an in-memory ``AsyncScheduler``
**silently** — persistence and cross-process coordination gone with nothing
turning red. And it is a **cron rather than a work queue**: its ``Scheduler``
ABC knows ``add_service(ScheduledService)`` with a trigger, and no single
enqueue, no per-job progress, no cancel, no lease. It becomes worth wiring up
for a PostgreSQL deployment, where the persistence it wants is really there;
on a desktop it would be a dependency that quietly does nothing.
"""

import asyncio
import contextlib
import logging

from appkit_commons.database.session import get_asyncdb_session

from mailarc_sync.engine.registry import ProviderRegistry
from mailarc_sync.jobs.queue import JobQueue, SessionFactory
from mailarc_sync.jobs.sweep import MAILBOX_KINDS, MailboxSweep

logger = logging.getLogger(__name__)

__all__ = ["MAILBOX_KINDS", "IntervalScheduler"]
"""``MAILBOX_KINDS`` is re-exported because it moved: it is the schedule's own
statement of what rules out a sweep, and the callers that read it off this
module were reading the right thing from what is now the wrong place."""


class IntervalScheduler:
    """Wakes every *interval_seconds* and queues a delta for the mailboxes that owe one.

    It enqueues and nothing else. A sweep touches two tables — it reads
    ``mail_accounts`` and it inserts into ``mail_sync_jobs`` — and everything
    that follows is the worker loop's ordinary business: the job is claimed
    under a lease, reports progress into its row and can be cancelled from the
    UI, exactly like the one a human queues by pressing a button.

    It cannot take the worker down. A sweep that throws is logged and the loop
    goes back to waiting; an account that throws is logged and the next account
    is tried. The reason is the failure mode this replaces: a single mailbox
    with a revoked token would otherwise end the schedule for every other
    mailbox, and nothing would say so until somebody noticed that no mail had
    arrived for a week.
    """

    def __init__(
        self,
        queue: JobQueue,
        registry: ProviderRegistry,
        *,
        interval_seconds: float,
        session_factory: SessionFactory = get_asyncdb_session,
    ) -> None:
        self._interval = interval_seconds
        self._sweep_of = MailboxSweep(queue, registry, session_factory=session_factory)
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        """Sweep every *interval_seconds* until asked to stop; zero means never.

        Zero is the default and it returns here, before anything is read: a
        fresh install must not start talking to somebody's mailbox on its own,
        and the first sync is a button a human presses.

        The wait comes before the first sweep rather than after it. A desktop
        application restarts often — every reload, every crash, every close of
        the lid — and sweeping on startup would turn each of those into a round
        of syncs. Whatever a restart missed is at most one interval old and the
        next sweep collects it.
        """
        if self._interval <= 0:
            logger.info(
                "No incremental schedule configured; new mail waits for a human"
            )
            return
        logger.info("Looking for new mail every %.0f seconds", self._interval)
        try:
            while not self._stopping.is_set():
                await self._wait()
                if self._stopping.is_set():
                    break
                await self._sweep()
        finally:
            logger.info("The incremental schedule stopped")

    def request_stop(self) -> None:
        """Ask the loop to finish. Safe to call from a signal handler.

        Sets a flag and returns, like :meth:`JobWorker.request_stop`, and the
        flag is also what the interval waits on — so a schedule parked on a
        fifteen-minute wait stops in the time it takes to run one ``if``
        instead of a quarter of an hour later.
        """
        if not self._stopping.is_set():
            logger.debug("The incremental schedule was asked to stop")
        self._stopping.set()

    async def tick(self) -> list[int]:
        """One sweep: the ids of the jobs it queued.

        Public so a test can drive a single sweep instead of racing a loop, and
        so an operator's script can. It has no side effect beyond the jobs it
        returns.
        """
        return await self._sweep_of.deltas()

    async def _wait(self) -> None:
        """Wait out the interval, or return early because a stop was asked for."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), self._interval)

    async def _sweep(self) -> None:
        """One :meth:`tick`, with the loop's own life insured against it.

        The database is the thing that goes away here — a locked SQLite file, a
        directory that vanished under a laptop's sleep — and none of that is a
        reason to stop looking for mail for the rest of the process's life.
        """
        try:
            queued = await self.tick()
        except Exception:
            logger.exception("A sweep for new mail failed; the schedule carries on")
            return
        if queued:
            logger.info("Queued %d incremental job(s): %s", len(queued), queued)
