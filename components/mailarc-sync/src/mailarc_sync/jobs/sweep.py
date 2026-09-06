"""Which mailboxes owe a run, and the jobs that give them one.

The *who*, split off from :mod:`mailarc_sync.jobs.scheduler`'s *when*. Both
sweeps ask a mailbox the same four questions — is it enabled, is it already
running something, is it waiting for a human's re-consent, can this process
open its provider at all — and they differ only in what they do with the
answers:

:meth:`MailboxSweep.deltas`
    what the schedule queues. A delta for every *armed* mailbox and nothing
    else: a timer that queued a full import on its own would walk twenty years
    of somebody's mail because a laptop woke up.

:meth:`MailboxSweep.everything`
    what the rail's Sync button queues. Every enabled mailbox gets a run — a
    delta where one can start, a full import where none can — because "sync my
    accounts" is a sentence about all of them, and the mailbox nobody has
    imported yet is exactly the one the person pressing it is waiting for.
    A full import is not a re-download either: the engine skips what is already
    archived (``ImportEngine._unarchived``), so the button's promise is the
    unarchived mail rather than the mailbox.

Its own module rather than two public methods on the scheduler, for the reason
the scheduler's docstring already gives about the queue: a class that answers
"when" has no business also being the thing a button holds, and a UI forced to
build an ``IntervalScheduler(interval_seconds=0)`` in order to press it would
be saying so out loud. The loop keeps :mod:`~mailarc_sync.jobs.scheduler`; the
per-mailbox decisions live here, once, so the two sweeps cannot drift on what
an account in ``auth_error`` deserves.

Nothing here opens a mailbox. The *descriptor* is asked whether a provider does
deltas — never the source — so deciding to sync never costs a connection.
"""

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


class SweepReport(BaseModel):
    """What one sweep did, in the three answers a mailbox can give.

    Frozen, and counts rather than prose: the sentence a person reads is the
    UI's to write, and a report that carried one would decide how an archive
    with no accounts at all differs from one whose only account is mid-import.

    A mailbox appears in exactly one bucket. ``blocked`` is the one worth
    naming: it means *a human has to do something first* — grant consent again,
    or install the component that opens this provider — and it is separate from
    ``busy`` because waiting is not the same news as being told to wait.
    """

    model_config = ConfigDict(frozen=True)

    queued: tuple[int, ...] = ()
    """The ids of the jobs this sweep put in the queue."""

    busy: tuple[int, ...] = ()
    """Accounts left alone because a mailbox job of theirs is still open."""

    blocked: tuple[int, ...] = ()
    """Accounts nothing can be queued for until a human acts."""

    @property
    def considered(self) -> int:
        """How many mailboxes the sweep looked at — zero means none exist."""
        return len(self.queued) + len(self.busy) + len(self.blocked)


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


class MailboxSweep:
    """Reads two tables, writes one, and never opens a mailbox.

    It enqueues and nothing else. Everything that follows is the worker loop's
    ordinary business: the job is claimed under a lease, reports progress into
    its row and can be cancelled from the UI, exactly like the one a human
    queues from the accounts page.

    Neither sweep can be taken down by one mailbox. An account that throws is
    logged and the next account is tried, because the failure mode this
    replaces is a single revoked token ending the sync for every other mailbox
    with nothing saying so until somebody notices no mail has arrived.
    """

    def __init__(
        self,
        queue: JobQueue,
        registry: ProviderRegistry,
        *,
        session_factory: SessionFactory = get_asyncdb_session,
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._session_factory = session_factory
        self._accounts = MailAccountRepository()
        self._checkpoints = SyncCheckpointRepository()

    async def deltas(self) -> list[int]:
        """One scheduled sweep: the ids of the jobs it queued.

        Returns a bare list because that is all a timer has to say — nobody
        reads a log line to find out that an unarmed mailbox stayed unarmed.
        """
        report = await self._run(full_import=False)
        return list(report.queued)

    async def everything(self) -> SweepReport:
        """One human's sweep: a run for every enabled mailbox that can take one.

        The report is the difference from :meth:`deltas`. A button that queued
        nothing has to be able to say *why* — every mailbox is already syncing,
        or every mailbox is waiting for a re-consent, or there are no mailboxes
        — and "no jobs" answers none of those.
        """
        return await self._run(full_import=True)

    async def _run(self, *, full_import: bool) -> SweepReport:
        """Both sweeps, differing only in what an unarmed mailbox is owed."""
        queued: list[int] = []
        busy: list[int] = []
        blocked: list[int] = []
        for candidate in await self._candidates():
            try:
                await self._sort(candidate, full_import, queued, busy, blocked)
            except Exception:
                # One mailbox, one failure. The next account is somebody else's
                # mail and has done nothing wrong.
                logger.exception(
                    "Could not schedule account %d (%s)",
                    candidate.account_id,
                    candidate.address,
                )
        return SweepReport(
            queued=tuple(queued), busy=tuple(busy), blocked=tuple(blocked)
        )

    async def _sort(
        self,
        candidate: _Candidate,
        full_import: bool,
        queued: list[int],
        busy: list[int],
        blocked: list[int],
    ) -> None:
        """Put one mailbox in its bucket, queueing a job if it earns one.

        Cheapest question first, and the two that cost a query last: an account
        that cannot be synced at all should not also cost two ``SELECT``s every
        interval.
        """
        if candidate.status == AccountStatus.AUTH_ERROR:
            # Nothing but a human's re-consent moves an account out of this
            # state, so a sweep that queued one anyway would fail a job, hammer
            # the provider and fill the job table with identical failures —
            # every interval, for as long as it takes somebody to notice. A
            # button gets the same answer, and says it out loud.
            logger.debug(
                "Account %d is waiting for a re-consent; not scheduling it",
                candidate.account_id,
            )
            blocked.append(candidate.account_id)
            return
        does_deltas = self._does_deltas(candidate)
        if does_deltas is None:
            blocked.append(candidate.account_id)
            return
        if await self._is_open(candidate):
            busy.append(candidate.account_id)
            return
        kind = await self._kind_for(candidate, does_deltas, full_import)
        if kind is None:
            return
        queued.append(await self._queue.enqueue(kind, candidate.account_id))

    async def _kind_for(
        self, candidate: _Candidate, does_deltas: bool, full_import: bool
    ) -> JobKind | None:
        """A delta where one can start, a full import where the caller allows it.

        ``None`` is the schedule's answer to a mailbox it may not touch, and it
        is deliberately not a bucket in the report: the timer has no reader.
        """
        if does_deltas and await self._is_armed(candidate):
            return JobKind.INCREMENTAL
        if not full_import:
            return None
        return JobKind.IMPORT

    async def _is_open(self, candidate: _Candidate) -> bool:
        """Whether a job that walks this mailbox is already going."""
        for kind in MAILBOX_KINDS:
            if await self._queue.find_open(kind, candidate.account_id) is not None:
                logger.debug(
                    "Account %d already has an open %s job", candidate.account_id, kind
                )
                return True
        return False

    async def _candidates(self) -> list[_Candidate]:
        """The accounts a sweep may touch, detached from their session."""
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
        does — and the button, finding the same "no", answers it with the full
        import that arms the mailbox for every sweep after.
        """
        async with self._session_factory() as session:
            checkpoint = await self._checkpoints.find_by_account_and_scope(
                session, candidate.account_id, INCREMENTAL_SCOPE
            )
        if checkpoint is not None and checkpoint.cursor is not None:
            return True
        logger.debug(
            "Account %d has never finished an import; a delta has nowhere to start",
            candidate.account_id,
        )
        return False

    def _does_deltas(self, candidate: _Candidate) -> bool | None:
        """Whether this provider answers "what changed since?" — ``None`` if unknown.

        The descriptor is asked, never the source: this module may not name a
        provider (§4.1), and it does not have to — ``supports_incremental`` is
        declared next to the provider's ``watermark()`` for exactly this
        question.

        The three-valued answer is what keeps a *missing* provider out of the
        "full import will do" branch. An account naming a provider this process
        never registered is a configuration problem rather than a mailbox
        problem, and it is a real one: a build without the Gmail component
        still has the Gmail rows. No job of any kind can run for it, so the
        button reports it as blocked rather than queueing an import that would
        fail the moment a worker claimed it.
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
            return None
        if not descriptor.supports_incremental:
            logger.debug(
                "%s has no delta; account %d is left to its full imports",
                provider,
                candidate.account_id,
            )
        return descriptor.supports_incremental
