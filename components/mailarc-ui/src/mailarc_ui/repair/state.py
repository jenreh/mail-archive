"""Reflex state for the conversation repair: one count, one job, one follow.

The shape :mod:`mailarc_ui.insights.state` established for the derived
rebuild, with everything the smaller job does not need taken out. The panel
asks the archive how many messages sit in no conversation, queues a ``repair``
job when somebody presses the button, and follows the row until it ends — then
asks again, because the number it was showing is the number the job just
changed.

The collaborators are looked up **inside** the handler that needs one, never
at import: ``mailarc-ui`` may not import ``app`` (§6), so everything the
composition root built arrives through the registry, and a module-level lookup
would run while ``app/app.py`` is still being imported.
"""

import asyncio
import logging

import reflex as rx
from reflex.event import EventCallback

from mailarc_sync.jobs import JobKind, JobQueue
from mailarc_ui.message_detail import archive_reader
from mailarc_ui.repair.model import (
    NO_JOB,
    RepairJobView,
    outside_label,
    repaired_label,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2
"""Seconds between two reads of the job row while a repair is running.

Faster than the rebuild's, because a repair is over in seconds on the archives
this exists for: a person who pressed the button would otherwise watch a
finished job for longer than it ran.
"""

POLL_TICKS_ALLOWED = 900
"""How many ticks a panel follows one job before it gives up on it.

Half an hour at :data:`POLL_INTERVAL`, which is far past any repair and is
here for the case that has nothing to do with the work: a page left open on a
job whose worker died must stop asking about it eventually.
"""

READ_FAILED = (
    "The archive could not be asked how many messages sit outside a "
    "conversation. Its graph did not answer — check that the mail archive is "
    "running. The details are in the application log."
)
"""What a failed read says, and never the driver's own text.

The refusal every panel in this application makes, for its stated reason: a
driver's message carries a path out of this installation, and this page renders
what it is given into a browser.
"""

GAVE_UP = "This page stopped following the repair. Reload to look again."


class ConversationRepairState(rx.State):
    """How much of the archive is outside a conversation, and the fix for it."""

    outside: int = 0
    checked: bool = False
    loading: bool = False
    error: str = ""

    job: RepairJobView = NO_JOB
    job_id: int = 0
    starting: bool = False
    polling: bool = False
    poll_interval: int = POLL_INTERVAL
    poll_ticks_allowed: int = POLL_TICKS_ALLOWED
    notice: str = ""

    @rx.var
    def outside_message(self) -> str:
        """The one sentence the panel leads with."""
        return outside_label(self.outside)

    @rx.var
    def can_repair(self) -> bool:
        """Whether pressing the button now would mean anything.

        Not while one is running, and not before the archive has answered:
        a button offered over an unread count is a button that might be
        offering to repair nothing.
        """
        return self.checked and not self.job.active and not self.starting

    @rx.var
    def result_message(self) -> str:
        """What the finished job did, or nothing at all.

        Only for a job that reached the end and wrote its tally. One that is
        still walking has nothing to say, and one that failed says so through
        :attr:`RepairJobView.error` instead of through a row of zeroes.
        """
        if self.job.active or not self.job.reported:
            return ""
        return repaired_label(self.job.filed, self.job.scanned, self.job.unkeyed)

    @rx.event(background=True)
    async def load(self) -> None:
        """Ask the archive how much of it is outside a conversation.

        The panel's ``on_load``, and what every finished repair ends with. In
        the background because it is two counts over the whole graph, and a
        page that blocked its own load on them would be a blank administration
        while the archive is large.
        """
        async with self:
            self.loading = True
        outside, error = await self._read_outside()
        async with self:
            self.outside = outside
            self.error = error
            self.checked = not error
            self.loading = False

    @rx.event
    async def start_repair(self) -> EventCallback[()] | None:
        """Queue a repair and start following it.

        No account: a conversation is keyed per mailbox, but the walk is over
        the whole archive, so a ``repair`` job carries no ``account_id``. The
        worker runs it; this page only asks and then watches the row.
        """
        await self._sync_job()
        if not self.job.active:
            await self._adopt_open_repair()
        if self.job.active:
            self.notice = "A repair is already running."
            return self._follow()
        self.starting = True
        try:
            self.job_id = await self._queue().enqueue(JobKind.REPAIR)
        except Exception as error:
            logger.exception("Could not queue a repair")
            self.notice = f"The repair could not be queued: {error}"
            return None
        finally:
            self.starting = False
        self.notice = ""
        await self._sync_job()
        logger.info("Started repair job %d", self.job_id)
        return self._follow()

    @rx.event
    def stop_polling(self) -> None:
        """Stop following the repair — the page is going away.

        Wired to the panel's ``on_unmount``. Without it a person who navigated
        away mid-repair would leave a background task reading the job row every
        :attr:`poll_interval` for the life of the session.
        """
        self.polling = False

    @rx.event(background=True)
    async def poll(self) -> None:
        """Follow the repair to its end, then read the count again.

        The lock is held around the state mutations and never around the read
        or the sleep. Every way out of the loop is an end: the flag going down,
        the job reaching a final state, the job disappearing from the queue,
        and the tick ceiling. A dropped read is the one thing that is not an
        end; the next tick asks again.

        Succeeded, failed and cancelled all re-read the count, because all
        three can leave the archive with fewer messages outside a conversation
        than the panel is showing — a repair that stopped halfway still wrote
        the pages it reached.
        """
        ticks = 0
        while True:
            async with self:
                if not self.polling or self.job_id <= 0:
                    self.polling = False
                    return
                if ticks >= self.poll_ticks_allowed:
                    self.polling = False
                    self.notice = GAVE_UP
                    logger.info(
                        "Stopped following repair job %d after %d ticks",
                        self.job_id,
                        ticks,
                    )
                    return
                job_id = self.job_id
            ticks += 1
            dropped = False
            try:
                reading = await self._read_job(job_id)
            except Exception:
                logger.exception("Repair job poll failed")
                reading, dropped = None, True
            async with self:
                if not self.polling:
                    return
                if not dropped:
                    self.job = reading or NO_JOB
                    if not self.job.active:
                        logger.info(
                            "Repair job %d ended as %s", job_id, self.job.status
                        )
                        self.polling = False
                        self.loading = True
                        break
            await asyncio.sleep(self.poll_interval)
        outside, error = await self._read_outside()
        async with self:
            self.outside = outside
            self.error = error
            self.checked = not error
            self.loading = False

    async def _read_outside(self) -> tuple[int, str]:
        """The count, or the sentence to show instead of one.

        Blocking — every runic driver is — so it goes to a thread. What comes
        back from a failure is :data:`READ_FAILED` and never the exception's
        own text; the traceback is logged first.
        """
        try:
            return await asyncio.to_thread(
                archive_reader().count_unthreaded_messages
            ), ""
        except Exception:
            logger.exception("Could not count the messages outside a conversation")
            return 0, READ_FAILED

    def _follow(self) -> EventCallback[()] | None:
        """Start watching the repair, unless this page already is."""
        if self.polling:
            return None
        self.polling = True
        return ConversationRepairState.poll

    async def _adopt_open_repair(self) -> None:
        """Take over a repair somebody else queued rather than adding one.

        ``job_id`` is per page and starts at zero, so "is one already running?"
        cannot be answered from this state alone: a second tab, or the same tab
        after a reload, knows about no repair and would queue its own. Two of
        them interleaving is harmless — the walk is idempotent — but the second
        would find nothing to do and report a repair that did nothing, which is
        a worse thing to show than the truth.
        """
        try:
            open_job = await self._queue().find_open(JobKind.REPAIR)
        except Exception:
            logger.exception("Could not ask the queue for an open repair")
            return
        if open_job is None:
            return
        self.job = RepairJobView.from_job(open_job)
        self.job_id = self.job.job_id
        logger.info("Following repair job %d, queued elsewhere", self.job_id)

    def _queue(self) -> JobQueue:
        """Built per call, never at import: the session factory it defaults to
        is configured while the application starts."""
        return JobQueue()

    async def _read_job(self, job_id: int) -> RepairJobView | None:
        """The watched job as this panel shows it, or nothing if it is gone."""
        job = await self._queue().get(job_id)
        return None if job is None else RepairJobView.from_job(job)

    async def _sync_job(self) -> None:
        """One read of the watched job, applied.

        A read that fails leaves the last reading standing rather than blanking
        the control: a database hiccup is not news that the repair ended.
        """
        if self.job_id <= 0:
            return
        try:
            self.job = await self._read_job(self.job_id) or NO_JOB
        except Exception:
            logger.exception("Could not read repair job %d", self.job_id)
