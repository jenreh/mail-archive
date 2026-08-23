"""The recurring trigger: which mailbox should be looked at again, and when.

Beside :mod:`mailarc_sync.jobs.queue` rather than inside it. The queue is
deliberately not a scheduler (§7.2) — it decides who may run a job and what
became of it, never that one should exist. This module is the other half of
that sentence and knows only the *when*: it enqueues and returns. Nothing here
opens a mailbox, and nothing here can fail a job, because at the moment it
runs there is no job yet.

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
from pydantic import BaseModel, ConfigDict

from mailarc_core.database.entities import AccountStatus
from mailarc_core.database.repositories import (
    MailAccountRepository,
    SyncCheckpointRepository,
)
from mailarc_core.mail.model import MailProvider
from mailarc_sync.engine.engine import INCREMENTAL_SCOPE
from mailarc_sync.engine.registry import ProviderRegistry, UnknownProviderError
from mailarc_sync.jobs.model import JobKind
from mailarc_sync.jobs.queue import JobQueue, SessionFactory

logger = logging.getLogger(__name__)

MAILBOX_KINDS = (JobKind.INCREMENTAL, JobKind.IMPORT)
"""The kinds that walk a mailbox, and therefore rule each other out.

An open ``incremental`` is the obvious one to wait for. An open ``import`` is
the one that is easy to miss and worse to get wrong: both kinds write the same
``mail_sync_checkpoints`` row for the full scope and both insert into
``mail_archived_messages``, whose unique key on (account, message) turns the
second writer's batch into an ``IntegrityError`` rather than a harmless
duplicate. A human who just pressed "Import" must not have their run broken by
a sweep that happened to land in the middle of it.
"""


class _Candidate(BaseModel):
    """One enabled account, copied off its row before the session closes.

    A value rather than the entity: the decisions below are made outside the
    session that read it, and a detached ORM instance answers questions with a
    ``DetachedInstanceError`` instead of with data.
    """

    model_config = ConfigDict(frozen=True)

    account_id: int
    provider: str
    """As stored — a plain string, because the table deliberately does not know
    the domain's provider names and a row may well name one this build has
    never heard of."""

    address: str
    status: str


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
        self._queue = queue
        self._registry = registry
        self._interval = interval_seconds
        self._session_factory = session_factory
        self._accounts = MailAccountRepository()
        self._checkpoints = SyncCheckpointRepository()
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
        queued: list[int] = []
        for candidate in await self._candidates():
            try:
                job_id = await self._job_for(candidate)
            except Exception:
                # One mailbox, one failure. The next account is somebody else's
                # mail and has done nothing wrong.
                logger.exception(
                    "Could not schedule account %d (%s)",
                    candidate.account_id,
                    candidate.address,
                )
                continue
            if job_id is not None:
                queued.append(job_id)
        return queued

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

    async def _candidates(self) -> list[_Candidate]:
        """The accounts a scheduled run may touch, detached from their session."""
        async with self._session_factory() as session:
            return [
                _Candidate(
                    account_id=account.id,
                    provider=account.provider,
                    address=account.email_address,
                    status=account.status,
                )
                for account in await self._accounts.find_enabled(session)
            ]

    async def _job_for(self, candidate: _Candidate) -> int | None:
        """Queue this account's delta, or ``None`` because it is being left alone.

        Cheapest question first, and the two that cost a query last: an account
        that cannot be synced at all should not also cost two ``SELECT``s every
        interval.
        """
        if candidate.status == AccountStatus.AUTH_ERROR:
            # Nothing but a human's re-consent moves an account out of this
            # state, so a sweep that queued one anyway would fail a job, hammer
            # the provider and fill the job table with identical failures —
            # every interval, for as long as it takes somebody to notice.
            logger.debug(
                "Account %d is waiting for a re-consent; not scheduling it",
                candidate.account_id,
            )
            return None
        if not self._does_deltas(candidate):
            return None
        if not await self._is_armed(candidate):
            return None
        for kind in MAILBOX_KINDS:
            if await self._queue.find_open(kind, candidate.account_id) is not None:
                logger.debug(
                    "Account %d already has an open %s job", candidate.account_id, kind
                )
                return None
        return await self._queue.enqueue(JobKind.INCREMENTAL, candidate.account_id)

    async def _is_armed(self, candidate: _Candidate) -> bool:
        """Whether some run has already left a point a delta may start at.

        The precondition that is easy to leave out and expensive to leave out:
        a delta over a mailbox nobody has walked has no history to ask about,
        so the engine bootstraps at today's watermark, archives nothing and
        reports success. Every sweep after that fetches only mail newer than
        the sweep that armed it, and whatever was in the mailbox beforehand —
        twenty years of it — is in no run at all. Nothing surfaces that: the
        job row says succeeded, the account shows no error, and the only trace
        is one INFO line the first time.

        The signal is the incremental checkpoint, because that is exactly what
        :meth:`ImportEngine.run` promises to leave behind when a walk finishes
        uncancelled — the same row the delta would read. A full-scope row would
        be the wrong question twice: an import that was cancelled halfway
        leaves one behind and a mailbox that was imported and then re-imported
        halfway would stop getting deltas it has every right to.

        So the schedule waits for the button, which is what the manual says it
        does. It says nothing at INFO the first time either, because "no
        checkpoint yet" is the ordinary state of an account somebody added five
        minutes ago and a warning about it every interval would train a reader
        to skip the log.
        """
        async with self._session_factory() as session:
            checkpoint = await self._checkpoints.find_by_account_and_scope(
                session, candidate.account_id, INCREMENTAL_SCOPE
            )
        if checkpoint is not None and checkpoint.cursor is not None:
            return True
        logger.debug(
            "Account %d has never finished an import; the first one is a button",
            candidate.account_id,
        )
        return False

    def _does_deltas(self, candidate: _Candidate) -> bool:
        """Whether this account's provider can answer "what changed since?".

        The descriptor is asked, never the source: this module may not name a
        provider (§4.1), and it does not have to — ``supports_incremental`` is
        declared next to the provider's ``watermark()`` for exactly this
        question, and this is the first consumer of it outside a test.

        An account naming a provider this process never registered is a
        configuration problem rather than a mailbox problem, and it is a real
        one: a build without the Gmail component still has the Gmail rows.
        Warned about once per interval and skipped, because there is no job
        that could carry the message to a human — a job of a kind nobody can
        run is worse than a log line.
        """
        try:
            provider = MailProvider(candidate.provider)
            descriptor = self._registry.descriptor_for(provider)
        except UnknownProviderError, ValueError:
            logger.warning(
                "Account %d names provider %r, which this process cannot open",
                candidate.account_id,
                candidate.provider,
            )
            return False
        if not descriptor.supports_incremental:
            logger.debug(
                "%s has no delta; account %d is left to its full imports",
                provider,
                candidate.account_id,
            )
            return False
        return True
