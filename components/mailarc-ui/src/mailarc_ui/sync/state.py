"""Reflex state behind the rail's Sync button: one press, every mailbox.

Three decisions, each with an obvious alternative that is wrong here.

**The sweep is not written in this module.** Deciding what a mailbox is owed —
a delta, a first full import, or nothing until a human re-consents — is
:class:`~mailarc_sync.jobs.sweep.MailboxSweep`'s, and it is the *same* decision
the worker's schedule makes on a timer. A copy of it here would be a second
policy that drifts, and the one that drifted would drift quietly: both spellings
queue jobs that succeed.

**"Syncing" is a fact about the archive, not about this button.** The spinner
follows every open mailbox job, whoever queued it — this press, the accounts
page's Import, or the schedule while nobody was looking — because that is what
somebody looking at a rail on a fresh page load is asking. Following only the
jobs *this* state queued would leave the rail still on a reload, halfway
through an hour-long import.

**The news arrives in the tooltip.** The rail is 76px wide and has no room for
a sentence, so :attr:`SyncAllState.hint` is where a press that queued nothing
gets to say why — every mailbox already running, or every mailbox waiting for a
re-consent, or no mailboxes at all. A button that answered those three with the
same silence would read as broken.

**And twice in a toast, because nobody watches a tooltip.** A tooltip is only
read by somebody already pointing at the button, and the two moments worth
telling them about are the two they are not: the press itself, which otherwise
does nothing visible but start a slow rotation, and the end of the run
minutes later, which otherwise is a rotation quietly stopping on a page they
have long since navigated away from. The end is the one that needs a count —
what a sync was *for* is the mail it brought in — so the ids this press queued
are kept in :attr:`SyncAllState.watching` and read back once the archive goes
quiet. Only this press's jobs: the count has to be an answer to the button
somebody pressed, not to everything the archive happened to be doing.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence

import reflex as rx
from reflex.event import EventCallback, EventSpec

from mailarc_sync.jobs import (
    MAILBOX_KINDS,
    JobQueue,
    MailboxSweep,
    SweepReport,
    SyncJob,
)
from mailarc_ui.accounts.state import provider_registry
from mailarc_ui.kit import MessageTone, toast

logger = logging.getLogger(__name__)

SYNC_LABEL = "Sync all accounts"
"""What the button is called when it is doing nothing — and its ARIA name.

The accessible name stays this whatever the tooltip currently says: a control
whose name changes under a screen reader while its behaviour does not is a
different control every time somebody tabs to it.
"""

NOTHING_TO_SYNC = "No mail accounts to sync yet."
"""An archive with no enabled mailbox. Its own sentence rather than a count of
zero, because it is the one outcome a person fixes on a different page."""

POLL_SECONDS = 3
"""How often the rail asks whether the archive is still syncing.

Slower than the import panel's two seconds on purpose: this is one boolean and
a count behind a spinner, not a progress bar somebody is watching.
"""


def mailboxes(count: int) -> str:
    """``1 mailbox`` / ``4 mailboxes`` — the phrase every sentence here counts in."""
    return "1 mailbox" if count == 1 else f"{count} mailboxes"


def messages(count: int) -> str:
    """``1 message`` / ``57 messages`` — what a finished run is measured in."""
    return "1 message" if count == 1 else f"{count} messages"


def announce(report: SweepReport) -> str:
    """What one press did, in the terms the person who pressed it asked in.

    A pure function over the report so the wording is testable without a state,
    a queue or a database — and so the three "queued nothing" outcomes stay
    visibly different from each other, which is the whole reason the sweep
    returns a report instead of a list of ids.
    """
    if report.considered == 0:
        return NOTHING_TO_SYNC
    clauses = []
    if report.queued:
        clauses.append(f"Syncing {mailboxes(len(report.queued))}")
    if report.busy:
        clauses.append(f"{mailboxes(len(report.busy))} already syncing")
    if report.blocked:
        blocked = len(report.blocked)
        verb = "needs" if blocked == 1 else "need"
        clauses.append(f"{mailboxes(blocked)} {verb} attention")
    return f"{'; '.join(clauses)}." if clauses else NOTHING_TO_SYNC


def summarise(jobs: Sequence[SyncJob]) -> tuple[str, MessageTone]:
    """What a finished run brought in, and how good the news is.

    Pure over the jobs for the same reason :func:`announce` is pure over the
    report: the arithmetic is the part that can quietly be wrong, and it should
    be provable without a queue underneath it.

    ``done`` and ``failed`` are disjoint — a message is archived or it left a
    row in ``mail_failed_messages`` — so both are counted and neither is
    inferred from the other. A run that dropped mail is not a success even when
    most of it arrived, which is the whole reason the tone is decided here and
    not by which handler happens to send the toast.
    """
    archived = sum(job.progress.done for job in jobs)
    dropped = sum(job.progress.failed for job in jobs)
    sentence = f"Finished: synced {messages(archived)}"
    if dropped:
        return f"{sentence}, {dropped} failed.", "warning"
    return f"{sentence}.", "success"


class SyncAllState(rx.State):
    """Press it once; watch the whole archive until it is done.

    ``active`` is separate from ``syncing`` for the reason
    :class:`~mailarc_ui.imports.state.ImportJobState` keeps ``job_id`` apart
    from ``job``: the count is what the tooltip says and the flag is what the
    stylesheet animates, and a read that comes back empty must be able to stop
    the animation without also having to explain itself.
    """

    syncing: bool = False
    """Whether any mailbox job is open right now — what the spinner follows."""

    active: int = 0
    """How many, for the tooltip."""

    starting: bool = False
    """A press is in flight. Disables the button so a double click is one sweep."""

    message: str = ""
    """What the last press reported, until the next one replaces it.

    Written twice per run and by both halves of it, so that the tooltip and
    the toast never disagree: a toast is gone in six seconds and this is what
    is left afterwards, and "Syncing 2 mailboxes." still sitting under the
    cursor an hour after they finished would be the rail's only lie.
    """

    watching: list[int] = []
    """The jobs *this* press queued, until the run they belong to is over.

    Deliberately not the same set as the spinner follows. ``syncing`` is a fact
    about the archive and answers "is anything happening"; this answers "what
    did the button I pressed do", and the finished toast can only count the
    second. A press that queued nothing leaves it empty and gets no toast at
    the end, which is right: there is no run of its own to report on.
    """

    polling: bool = False
    poll_interval: int = POLL_SECONDS

    @rx.var
    def hint(self) -> str:
        """The tooltip: what is happening, else what just happened, else the name."""
        if self.syncing:
            return f"Syncing {mailboxes(self.active)}…"
        return self.message or SYNC_LABEL

    @rx.event
    async def adopt(self) -> EventCallback[()] | None:
        """The button's ``on_mount``: is the archive already syncing?

        Without it the rail is honest only on the page the button was pressed
        on. An import runs for hours and every navigation remounts this
        component, so "did somebody start something before I got here?" has to
        be asked on arrival — including on the first load after a restart,
        where this state knows about no job at all.
        """
        await self._refresh()
        return self._follow()

    @rx.event
    async def sync_all(self) -> list[EventSpec | EventCallback[()]]:
        """Queue a run for every mailbox that can take one, and follow them.

        Nothing here decides *which* mailboxes those are, and nothing here has
        to guard against queueing a second run over a mailbox that is already
        going: :meth:`MailboxSweep.everything` refuses that itself and says so
        in the report, which is the only place that question is answered.

        Returns a list because a press has two things to do at once — say what
        it did, and start following it — and the toast has to go out even when
        there is nothing to follow. That is exactly the press that most needs
        one: an archive with no accounts answers a click with no spinner, no
        change and, without this, no sentence anybody is looking at.
        """
        if self.starting:
            return []
        self.starting = True
        self.message = ""
        try:
            report = await self._sweep().everything()
        except Exception:
            # A press that cannot reach the database must not leave a button
            # that looks pressed. §7.6: nothing is dropped without a trace.
            logger.exception("Could not sync the accounts")
            self.message = "Could not start the sync — see the log."
            return [toast(self.message, "failure")]
        finally:
            self.starting = False
        self.message = announce(report)
        self.watching = [*self.watching, *report.queued]
        logger.info(
            "Sync queued %d job(s); %d already running, %d blocked",
            len(report.queued),
            len(report.busy),
            len(report.blocked),
        )
        await self._refresh()
        events: list[EventSpec | EventCallback[()]] = [
            toast(self.message, "note" if report.queued else "warning")
        ]
        follow = self._follow()
        if follow is not None:
            events.append(follow)
        return events

    @rx.event
    def stop_polling(self) -> None:
        """Let a page that goes away turn the poll off."""
        self.polling = False

    @rx.event(background=True)
    async def poll(self) -> AsyncIterator[EventSpec]:
        """Follow the archive until no mailbox job is open, then say what came in.

        The same shape as the import panel's loop and for the same reasons: the
        lock is held only around the state mutation, so the read and the sleep
        never block the rest of the app, and a read that fails is logged and
        retried rather than ending the loop — a spinner that stopped on one
        locked database would say the sync had finished.

        The one addition is the exit. A loop that ends because the archive went
        quiet leaves the lock, reads the jobs this press queued and yields the
        count; every other way out — the page went away, the state was told to
        stop — yields nothing, because nobody is there to read it. Reading
        those rows *outside* the lock matters as much here as in the loop body:
        it is one query per job against the same database the worker is
        writing to.
        """
        finished: list[int] = []
        while True:
            async with self:
                if not self.polling:
                    return
            try:
                open_jobs = await self._open_count()
            except Exception:
                logger.exception("Could not read the open sync jobs")
                open_jobs = None
            async with self:
                if not self.polling:
                    return
                if open_jobs is not None:
                    self._apply(open_jobs)
                    if not self.syncing:
                        logger.info("Nothing is syncing any more")
                        self.polling = False
                        finished = list(self.watching)
                        self.watching = []
                        break
            await asyncio.sleep(self.poll_interval)
        if finished:
            sentence, tone = summarise(await self._finished_jobs(finished))
            logger.info("Sync finished: %s", sentence)
            async with self:
                self.message = sentence
            yield toast(sentence, tone)

    def _follow(self) -> EventCallback[()] | None:
        """Start the poll, unless one is running or nothing is going on."""
        if self.polling or not self.syncing:
            return None
        self.polling = True
        return SyncAllState.poll

    async def _refresh(self) -> None:
        """One read, applied — for every handler that is not the poll loop.

        A failure leaves the flag where it was rather than clearing it: the
        archive did not stop syncing because this process could not ask.
        """
        try:
            self._apply(await self._open_count())
        except Exception:
            logger.exception("Could not read the open sync jobs")

    def _apply(self, open_jobs: int) -> None:
        self.active = open_jobs
        self.syncing = open_jobs > 0

    async def _finished_jobs(self, watched: Sequence[int]) -> list[SyncJob]:
        """Read back the jobs one press queued, for the count in the toast.

        A job that cannot be read is left out of the arithmetic rather than
        counted as zero and rather than sinking the sentence: the run really
        did happen, and "synced 40 messages" with one job missing is closer to
        the truth than silence. The gap is logged, which is where a job that
        vanished belongs.
        """
        queue = JobQueue()
        jobs: list[SyncJob] = []
        for job_id in watched:
            try:
                job = await queue.get(job_id)
            except Exception:
                logger.exception("Could not read finished sync job %d", job_id)
                continue
            if job is None:
                logger.warning("Finished sync job %d is no longer in the queue", job_id)
                continue
            jobs.append(job)
        return jobs

    async def _open_count(self) -> int:
        """How many mailbox jobs have not ended yet.

        One query rather than one per account: the rail asks this on every page
        load of an archive that may hold a dozen mailboxes.
        """
        return len(await JobQueue().find_all_open(MAILBOX_KINDS))

    def _sweep(self) -> MailboxSweep:
        """Built per call, never at import.

        The session factory and the provider registry are both filled while the
        app starts; one built at module level would capture the world too
        early — the same rule every state in this package follows.
        """
        return MailboxSweep(JobQueue(), provider_registry())
