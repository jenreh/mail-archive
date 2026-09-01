"""Reflex state for starting an import and watching it run.

The job queue owns the state machine, so nothing here re-implements it: this
asks it to enqueue, reads jobs back by id and asks it to stop one. What it adds
is the projection. A :class:`~mailarc_sync.jobs.model.SyncJob` carries leases,
worker ids and timestamps a browser has no use for, and §9.1 keeps rich objects
out of a Reflex state — so every job the panel shows is a small frozen row with
its percentage and its counts already turned into strings.
"""

import asyncio
import logging

import reflex as rx
from pydantic import BaseModel, ConfigDict
from reflex.event import EventCallback

from mailarc_sync.jobs import JobKind, JobProgress, JobQueue, JobState, SyncJob

logger = logging.getLogger(__name__)

_OPEN_STATES = (JobState.QUEUED, JobState.RUNNING)
"""The states a job can still leave by itself; the other three are ends."""

_RECENT_LIMIT = 5
"""How many jobs the panel keeps in view — phase 4 shows a handful, not a log."""

_NO_PERCENT = "—"
"""Stands in while the provider has not estimated a total.

A job without a total is not a job at zero and even less one at a hundred, so
it gets a dash rather than a number nobody can act on.
"""

_STATE_COLORS = {
    JobState.QUEUED: "gray",
    JobState.RUNNING: "blue",
    JobState.SUCCEEDED: "teal",
    JobState.FAILED: "red",
    JobState.CANCELLED: "orange",
}
"""What each state should look like — decided here, with the other labels.

A component picking the colour would have to match on a `Var`, and a badge is
no better a place to know the job vocabulary than a row already is.
"""


class ImportJobRow(BaseModel):
    """One job the way the panel shows it: numbers already made readable.

    A pydantic model, not `rx.Base` — that was removed in Reflex 0.9. Frozen,
    because a row is a reading and not a handle: anything that acts on a job
    goes through the queue by id.
    """

    model_config = ConfigDict(frozen=True)

    job_id: int = 0
    account_id: int = 0
    status: str = ""
    status_color: str = "gray"
    percent: float = 0.0
    percent_label: str = _NO_PERCENT
    counts_label: str = ""
    error: str = ""
    active: bool = False
    cancel_requested: bool = False

    @classmethod
    def from_job(cls, job: SyncJob) -> ImportJobRow:
        """Project a job onto the handful of values a browser needs."""
        percent = percent_of(job.progress)
        return cls(
            job_id=job.id,
            account_id=job.account_id or 0,
            status=str(job.state),
            status_color=_STATE_COLORS.get(job.state, "gray"),
            percent=percent,
            percent_label=(
                _NO_PERCENT if job.progress.total <= 0 else f"{percent:.0f}%"
            ),
            counts_label=counts_of(job.progress),
            error=job.error or "",
            active=job.state in _OPEN_STATES,
            cancel_requested=job.cancel_requested,
        )


_IDLE = ImportJobRow()
"""Nothing watched yet. A sentinel row keeps every component free of `None`."""


class ImportJobState(rx.State):
    """Start an import, follow it, stop it — all of phase 4's import UI.

    ``job_id`` is the job being watched and ``job`` is the last reading of it.
    They are separate on purpose: a read that comes back empty must not make
    the panel forget what it was following.
    """

    account_id: int = 0
    job_id: int = 0
    job: ImportJobRow = _IDLE
    recent: list[ImportJobRow] = []
    message: str = ""
    starting: bool = False
    cancelling: bool = False
    polling: bool = False
    poll_interval: int = 2

    _watched: dict[int, list[int]] = {}
    """Per mailbox, the ids this panel follows, newest first.

    Keyed by account because an import is a mailbox's, not the archive's: two
    can be open at once, and the panel shows the history of the mailbox it is
    pointed at rather than of whichever one ran last.
    """

    @rx.var
    def can_start(self) -> bool:
        """``job`` is the selected mailbox's job — see :meth:`select_account`.

        Which is what keeps a mailbox startable while another one imports.
        """
        return self.account_id > 0 and not self.job.active

    @rx.var
    def can_cancel(self) -> bool:
        return self.job.active and not self.job.cancel_requested

    @rx.var
    def has_job(self) -> bool:
        return self.job.job_id > 0

    @rx.var
    def has_recent(self) -> bool:
        return len(self.recent) > 0

    @rx.event
    async def select_account(self, account_id: int) -> EventCallback[()] | None:
        """Point the panel at an account; the page decides which one.

        The id is the smaller half. ``job`` drives both buttons, so a job left
        standing from the mailbox before this one would disable a start this
        mailbox is entitled to — it is dropped here and looked up again for
        the account now on screen.
        """
        self.account_id = account_id
        self.message = ""
        self.job_id = 0
        self.job = _IDLE
        await self._adopt_open_import()
        await self._sync()
        return self._follow()

    @rx.event
    async def refresh(self) -> EventCallback[()] | None:
        """Read the watched jobs back once — the page's ``on_load``.

        And pick an import back up if this mailbox is running one, so a reload
        finds the job it forgot instead of offering to queue a second.
        """
        await self._adopt_open_import()
        await self._sync()
        return self._follow()

    @rx.event
    async def start_import(self) -> EventCallback[()] | None:
        """Queue an import for the selected account and start following it.

        One import per mailbox, not one per archive: a mailbox that is idle
        can be queued while another one is still importing, and the worker
        takes them in turn.
        """
        if self.account_id <= 0:
            self.message = "Choose an account first."
            return None
        # A panel that has not polled for a while must not refuse a new import
        # over a job that ended while nobody was looking — nor add a second one
        # over a job this panel never saw being queued.
        await self._adopt_open_import()
        await self._sync()
        if self.job.active:
            self.message = "An import is already running for this mailbox."
            return None
        self.starting = True
        try:
            self.job_id = await self._queue().enqueue(JobKind.IMPORT, self.account_id)
        finally:
            self.starting = False
        self._watch(self.job_id)
        self.message = ""
        await self._sync()
        logger.info(
            "Started import job %d for account %d", self.job_id, self.account_id
        )
        return self._follow()

    @rx.event
    async def cancel_import(self) -> None:
        """Ask the job to stop — a flag, not a kill (§7.2).

        The worker reads it between batches, so whatever was half written when
        a human clicked is still written whole.
        """
        if self.job_id <= 0:
            return
        self.cancelling = True
        try:
            asked = await self._queue().request_cancel(self.job_id)
        finally:
            self.cancelling = False
        self.message = "" if asked else "That job had already ended."
        await self._sync()

    @rx.event
    def stop_polling(self) -> None:
        """Let a page that goes away turn the poll off."""
        self.polling = False

    @rx.event(background=True)
    async def poll(self) -> None:
        """Follow the watched job until it reaches an end state.

        The lock is held only around the state mutation; the read and the sleep
        happen outside it so the rest of the app is never blocked waiting on
        us. A job that has ended stops the loop — a panel left open overnight
        must not keep asking about it.
        """
        while True:
            async with self:
                if not self.polling or self.job_id <= 0:
                    self.polling = False
                    return
                watched = self._watched_ids()
            try:
                rows = await self._read(watched)
            except Exception:
                logger.exception("Import job poll failed")
                rows = None
            async with self:
                if not self.polling:
                    return
                if rows is not None:
                    self._apply(rows)
                    if not self.job.active:
                        logger.info("Job %d ended as %s", self.job_id, self.job.status)
                        self.polling = False
                        return
            await asyncio.sleep(self.poll_interval)

    def _follow(self) -> EventCallback[()] | None:
        """Start the poll, unless one is running or there is nothing to watch."""
        if self.polling or not self.job.active:
            return None
        self.polling = True
        return ImportJobState.poll

    async def _adopt_open_import(self) -> None:
        """Follow the import this mailbox is already running, whoever queued it.

        ``job_id`` is per page and starts at zero, so "is this mailbox already
        importing?" cannot be answered from this state alone: a second tab, or
        the same tab after a reload, knows about no job and would queue its own
        over one that is running.

        A queue that cannot answer is not a reason to refuse: the enqueue right
        after would fail too, and that path already has the message for it.
        """
        if self.account_id <= 0:
            return
        try:
            open_job = await self._queue().find_open(JobKind.IMPORT, self.account_id)
        except Exception:
            logger.exception("Could not ask the queue for an open import")
            return
        if open_job is None:
            return
        self.job_id = open_job.id
        self._watch(open_job.id)
        logger.info(
            "Following import job %d for account %d", open_job.id, self.account_id
        )

    def _watch(self, job_id: int) -> None:
        """Put a job at the front of its mailbox's history, once."""
        kept = [watched for watched in self._watched_ids() if watched != job_id]
        self._watched = {
            **self._watched,
            self.account_id: [job_id, *kept][:_RECENT_LIMIT],
        }

    def _watched_ids(self) -> list[int]:
        """The ids this panel follows for the mailbox that is on screen."""
        return list(self._watched.get(self.account_id, []))

    def _queue(self) -> JobQueue:
        """Built per call, never at import.

        The session factory it defaults to is configured while the app starts;
        a queue built at module level would capture the world too early.
        """
        return JobQueue()

    async def _read(self, watched: list[int]) -> list[ImportJobRow]:
        """Read the watched jobs back, newest first.

        Takes the ids as an argument rather than reading them off ``self``:
        the poll loop calls this outside its lock.
        """
        queue = self._queue()
        rows: list[ImportJobRow] = []
        for job_id in watched:
            job = await queue.get(job_id)
            if job is not None:
                rows.append(ImportJobRow.from_job(job))
        return rows

    def _apply(self, rows: list[ImportJobRow]) -> None:
        """Show what was read; the watched job is the one the controls act on."""
        self.recent = rows
        self.job = next((row for row in rows if row.job_id == self.job_id), _IDLE)

    async def _sync(self) -> None:
        """One read, applied — for every handler that is not the poll loop."""
        self._apply(await self._read(self._watched_ids()))


def percent_of(progress: JobProgress) -> float:
    """How far a job got, as a number a progress bar can take.

    ``done`` and ``failed`` are disjoint, so both count as handled. Without a
    total there is nothing to divide by and the answer is zero, not a hundred.
    The total may still grow while the job runs, hence the cap.
    """
    if progress.total <= 0:
        return 0.0
    return min(100.0, 100.0 * (progress.done + progress.failed) / progress.total)


def counts_of(progress: JobProgress) -> str:
    """The counters beside the bar, in the shortest honest wording."""
    seen = (
        f"{progress.done} / {progress.total}"
        if progress.total > 0
        else f"{progress.done} done"
    )
    return f"{seen} · {progress.failed} failed" if progress.failed else seen
