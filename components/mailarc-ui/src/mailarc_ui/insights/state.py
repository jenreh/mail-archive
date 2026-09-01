"""What the derived layer found, and whether to believe it.

The three analyses answer through
:class:`~mailarc_analytics.AnalyticsReader`, which the composition root builds
and leaves in the service registry — ``mailarc-ui`` may not import ``app``
(§4.1), so this reads the reader out the way the search page reads its archive,
inside a method and never at import time.

Two things make this a test rather than a report. A1 is read **twice**:
:meth:`~mailarc_analytics.AnalyticsReader.co_addressed_agreement` runs the
definition of co-addressing and the materialised ``CO_ADDRESSED`` edge against
each other, and what this state adds is the sentence a human acts on — the
direction of a disagreement is what says whether the edge is merely behind the
archive or claiming something no message supports. And a rebuild can be started
from here and watched, so changing something and seeing what it did to the
numbers is one page rather than a terminal and a reload.

Turning a catalogue row into the strings a table prints is the other half of
this page and lives in :mod:`mailarc_ui.insights.model`; what is left here is
everything that touches the graph, the job queue or the state lock. Every panel
carries its own loading flag and its own error string, because a graph that
goes away halfway through a refresh is a state four tables have to be able to
render — one dead panel must not take the other four with it.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable
from functools import partial

import reflex as rx
from appkit_commons.registry import service_registry
from reflex.event import EventCallback

from mailarc_analytics import (
    AnalyticsReader,
    ArchiveTotals,
    TemplateDirection,
)
from mailarc_sync.jobs import JobKind, JobQueue
from mailarc_ui.insights.model import (
    NO_AGREEMENT,
    NO_JOB,
    NO_TOTALS,
    AgreementView,
    GroupView,
    PairView,
    Readout,
    RebuildJobView,
    TemplateView,
    TopicView,
    TotalsView,
)

logger = logging.getLogger(__name__)

POLL_TICKS_ALLOWED = 900
"""Half an hour at the default two-second interval.

Long enough that a rebuild of a large archive is still being watched when it
ends, short enough that a page nobody is looking at stops asking. Giving up is
only ever a statement about this page: the job itself is untouched, and the
message says so.
"""

CANCEL_ASKED = "Cancellation requested — the worker will stop after the current stage."
"""What a cancel says about a rebuild a worker is running.

``request_cancel`` sets a flag and nothing else for a claimed job — the worker
is what honours it — so the panel goes on showing a running rebuild with both
buttons disabled: ``can_rebuild`` because the job is active, ``can_cancel``
because the flag is already set. Saying nothing left two dead buttons and no
reason for either.
"""

CANCEL_TOOK_EFFECT = "That rebuild was cancelled before any worker picked it up."
"""And what it says about one nothing had claimed.

``JobQueue.request_cancel`` ends an unclaimed job outright rather than flagging
it, because the half-written stage the flag exists to protect does not exist
yet. So the Rebuild button is live again, and the message has to say why
instead of promising a worker that never came.
"""


def _gave_up_on(job: RebuildJobView) -> str:
    """Why the page stopped following, in the words the situation deserves."""
    if job.status == "queued":
        return (
            "Not following this rebuild any more — no worker has picked it up. "
            "Start one with `task sync:worker`, then refresh."
        )
    return (
        "Not following this rebuild any more — it has been going for a while. "
        "Refresh to see where it got to."
    )


def analytics_reader() -> AnalyticsReader:
    """The reader the composition root published. Call inside a method only."""
    try:
        return service_registry().get(AnalyticsReader)
    except KeyError as error:
        raise RuntimeError(
            "No analytics reader is registered — did app.composition run?"
        ) from error


async def _answered[T](work: Callable[[], T], what: str, empty: T) -> tuple[T, str]:
    """One blocking read, off the event loop, with an outage as its answer.

    Every runic driver blocks, so the read goes to a thread. What comes back
    from a failure is a sentence for the panel that asked rather than an
    exception: a graph that went away is a state the page has to render, and
    one dead panel must not take the other four with it.
    """
    try:
        return await asyncio.to_thread(work), ""
    except Exception as error:
        logger.exception("Could not read %s", what)
        return empty, str(error) or type(error).__name__


def _projected[R, V](
    read: Callable[[], Iterable[R]], view: Callable[[R], V]
) -> Callable[[], list[V]]:
    """One panel's read *and* the projection of it, as a single unit of work.

    Both halves belong inside :func:`_answered` and only the read used to be.
    A projection is not the safe half: it renders dates, and a ``Date:`` header
    is whatever a sender wrote — one archived mail from the year 9999 raised
    ``OverflowError`` out of every listing at once and left the page spinning
    with no error text and no way back. Wrapping them together is what makes
    "one dead panel must not take the other four with it" true of the whole
    path from the graph to the table rather than of the graph call alone.
    """
    return lambda: [view(one) for one in read()]


class AnalyticsInsightsState(rx.State):
    """The insights page, and the cross-check that makes it a test.

    What the three analyses found, and whether A1's materialised edge still
    agrees with the archive it was derived from.

    ``job_id`` is the rebuild being watched and ``job`` is the last reading of
    it — separate for the reason the import panel keeps them separate: a read
    that comes back empty must not make the panel forget what it was
    following.
    """

    totals: TotalsView = NO_TOTALS
    agreement: AgreementView = NO_AGREEMENT
    pairs: list[PairView] = []
    groups: list[GroupView] = []
    topics: list[TopicView] = []
    sent_templates: list[TemplateView] = []
    received_templates: list[TemplateView] = []

    totals_error: str = ""
    agreement_error: str = ""
    groups_error: str = ""
    topics_error: str = ""
    templates_error: str = ""

    loading_totals: bool = True
    loading_agreement: bool = True
    loading_groups: bool = True
    loading_topics: bool = True
    loading_templates: bool = True
    """True to begin with, one per panel.

    The page renders before its first read returns, and an empty table is a
    claim about the archive that nothing has earned yet — a spinner is what the
    state actually knows at that moment. Every one of them is cleared by the
    same :meth:`_apply`, so a panel cannot be left spinning over data that
    arrived.
    """

    job_id: int = 0
    job: RebuildJobView = NO_JOB
    rebuild_message: str = ""
    starting: bool = False
    cancelling: bool = False
    polling: bool = False
    poll_interval: int = 2
    poll_ticks_allowed: int = POLL_TICKS_ALLOWED
    """How many times the poll asks before it stops following the rebuild.

    A loop whose only exits are "the job ended" and "the job vanished" has no
    exit at all when no worker is running — which is the normal state of a dev
    machine, and of any install where ``task sync:worker`` is not up. The job
    stays QUEUED, ``active`` stays True, and the loop ticks for ever.
    """

    @rx.var
    def has_archive(self) -> bool:
        return self.totals.messages > 0

    @rx.var
    def needs_rebuild(self) -> bool:
        """Mail is archived and nothing has been derived from it."""
        return self.totals.messages > 0 and self.totals.derived == 0

    @rx.var
    def guidance(self) -> str:
        """The one sentence a page with nothing to show has to say instead.

        Three different nothings — no graph, no mail, no rebuild — and four
        empty tables would answer none of them. Empty while the first read is
        still out, because "nothing is archived" is not what an unanswered
        count means, and empty when the graph failed, because the alert above
        already says that in the words the driver used.
        """
        if self.loading_totals or self.totals_error:
            return ""
        if self.totals.messages == 0:
            return (
                "Nothing is archived yet. Import a mailbox first — these "
                "analyses read the messages, not the mailbox."
            )
        if self.totals.derived == 0:
            return (
                f"{self.totals.messages} messages are archived and nothing has "
                "been derived from them yet. A rebuild reads the whole archive "
                "and writes the pairs, groups, topics and templates the panels "
                "below report on."
            )
        return ""

    @rx.var
    def busy(self) -> bool:
        """Whether any panel is still waiting on the graph."""
        return (
            self.loading_totals
            or self.loading_agreement
            or self.loading_groups
            or self.loading_topics
            or self.loading_templates
        )

    @rx.var
    def has_templates(self) -> bool:
        """Either direction found something — one panel holds both."""
        return bool(self.sent_templates or self.received_templates)

    @rx.var
    def can_rebuild(self) -> bool:
        return not self.job.active

    @rx.var
    def can_cancel(self) -> bool:
        return self.job.active and not self.job.cancel_requested

    @rx.var
    def has_job(self) -> bool:
        return self.job.job_id > 0

    @rx.event(background=True)
    async def load(self) -> None:
        """Read every panel. The page's ``on_load``, and its Refresh button.

        A background task, so the lock is held around the two mutations and
        never around the read. A1's half of this is a self-join over every
        message in the archive — the one read on this page whose cost grows
        with the mailbox — and a plain handler holds that client's state lock
        for its whole duration, which freezes every other event on the session
        behind a report. :class:`Readout` exists to make this shape possible;
        only :meth:`poll` used it.
        """
        async with self:
            self._reading()
        readout = await self._read_everything()
        async with self:
            self._apply(readout)

    @rx.event(background=True)
    async def check_agreement(self) -> None:
        """Run A1's two answers against each other again, and nothing else.

        The self-join over every message is the one expensive read on this
        page, and the one worth repeating on its own: a human who has just
        changed something in the write path wants the verdict again, not four
        listings that did not move. Off the lock for the same reason
        :meth:`load` is — it is the *more* expensive of the two.
        """
        async with self:
            self.loading_agreement = True
        verdict, listed, failure = NO_AGREEMENT, [], ""
        try:
            try:
                reader = analytics_reader()
            except RuntimeError as error:
                failure = str(error)
            else:
                verdict, listed, failure = await self._read_agreement(reader)
        finally:
            # A ``finally`` and not three assignments at the end: whatever gets
            # past `_answered` — and something eventually does — must not leave
            # this panel spinning over a read that is already over.
            async with self:
                self.agreement, self.pairs = verdict, listed
                self.agreement_error = failure
                self.loading_agreement = False

    @rx.event
    async def start_rebuild(self) -> EventCallback[()] | None:
        """Queue a rebuild of the derived layer and start following it.

        No account: a rebuild is about the whole archive, which is why a
        ``derive`` job carries no ``account_id``. The worker runs it; this page
        only asks and then watches the row.
        """
        await self._sync_job()
        if not self.job.active:
            await self._adopt_open_rebuild()
        if self.job.active:
            self.rebuild_message = "A rebuild is already running."
            return self._follow()
        self.starting = True
        try:
            self.job_id = await self._queue().enqueue(JobKind.DERIVE)
        except Exception as error:
            logger.exception("Could not queue a rebuild")
            self.rebuild_message = f"The rebuild could not be queued: {error}"
            return None
        finally:
            self.starting = False
        self.rebuild_message = ""
        await self._sync_job()
        logger.info("Started rebuild job %d", self.job_id)
        return self._follow()

    @rx.event
    async def cancel_rebuild(self) -> None:
        """Ask the rebuild to stop — a flag, not a kill (§7.2).

        The worker reads it between stages, so the stage that was running
        finishes and what is in the graph is whole stages of work. It is still
        half a derived layer, which is why the panels are read again whichever
        way the job ends.
        """
        if self.job_id <= 0:
            return
        self.cancelling = True
        try:
            asked = await self._queue().request_cancel(self.job_id)
        finally:
            self.cancelling = False
        if not asked:
            self.rebuild_message = "That rebuild had already ended."
            await self._sync_job()
            return
        await self._sync_job()
        # Two different things wear the same button. A job a worker is running
        # gets a flag it will read between stages; one nothing had claimed is
        # simply over, and the Rebuild button is live again — so saying "the
        # worker will stop after the current stage" over a control that just
        # came back would explain the wrong half.
        self.rebuild_message = CANCEL_ASKED if self.job.active else CANCEL_TOOK_EFFECT

    @rx.event
    def stop_polling(self) -> None:
        """Stop following the rebuild — the page is going away.

        Wired to ``insights_panel``'s ``on_unmount``. It used to be wired to
        nothing at all while its docstring claimed otherwise, so a user who
        navigated away during a rebuild left a background task hitting the
        database every :attr:`poll_interval` for the life of the session, one
        per abandoned page.
        """
        self.polling = False

    @rx.event(background=True)
    async def poll(self) -> None:
        """Follow the rebuild to its end, then read every panel again.

        The lock is held around the state mutations and never around the read
        or the sleep, so a rebuild that takes minutes never blocks the rest of
        the app. Every way out of the loop is an end: the flag going down, the
        job reaching a final state, and the job disappearing from the queue
        altogether — a panel left open overnight must not keep asking about a
        row that is gone. A dropped read is the one thing that is not an end;
        the next tick asks again.

        Succeeded, failed and cancelled all refresh the panels. A rebuild that
        stopped halfway leaves a half-written derived layer, and a page still
        showing the layer from before it would be showing something that is no
        longer in the graph.
        """
        ticks = 0
        while True:
            async with self:
                if not self.polling or self.job_id <= 0:
                    self.polling = False
                    return
                if ticks >= self.poll_ticks_allowed:
                    self.polling = False
                    self.rebuild_message = _gave_up_on(self.job)
                    logger.info(
                        "Stopped following rebuild job %d after %d ticks",
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
                logger.exception("Rebuild job poll failed")
                reading, dropped = None, True
            async with self:
                if not self.polling:
                    return
                if not dropped:
                    self.job = reading or NO_JOB
                    if not self.job.active:
                        logger.info(
                            "Rebuild job %d ended as %s", job_id, self.job.status
                        )
                        self.polling = False
                        self._reading()
                        break
            await asyncio.sleep(self.poll_interval)
        readout = await self._read_everything()
        async with self:
            self._apply(readout)

    async def _read_everything(self) -> Readout:
        """Ask every panel's question once, and let each one fail on its own.

        Totals first and alone: six counts over labels that may not exist yet,
        which is the cheap read that says whether any of the others is worth
        making. An archive nobody has rebuilt would answer four of them with
        nothing and the cross-check with every pair in the archive reported as
        a disagreement — true, expensive and useless — so that case stops here
        and :attr:`guidance` says what to do instead.
        """
        try:
            reader = analytics_reader()
        except RuntimeError as error:
            return Readout(totals_error=str(error))
        totals, totals_error = await _answered(
            reader.totals, "the archive totals", ArchiveTotals()
        )
        counted = TotalsView.from_totals(totals)
        if totals_error or counted.derived == 0:
            return Readout(totals=counted, totals_error=totals_error)
        agreement, pairs, agreement_error = await self._read_agreement(reader)
        groups, groups_error = await _answered(
            _projected(reader.recurring_groups, GroupView.from_row),
            "the recurring groups",
            [],
        )
        topics, topics_error = await _answered(
            _projected(reader.topics, TopicView.from_row), "the topics", []
        )
        sent, sent_error = await _answered(
            _projected(
                partial(reader.templates, TemplateDirection.SENT),
                TemplateView.from_row,
            ),
            "the sent templates",
            [],
        )
        received, received_error = await _answered(
            _projected(
                partial(reader.templates, TemplateDirection.RECEIVED),
                TemplateView.from_row,
            ),
            "the received templates",
            [],
        )
        return Readout(
            totals=counted,
            agreement=agreement,
            pairs=pairs,
            agreement_error=agreement_error,
            groups=groups,
            groups_error=groups_error,
            topics=topics,
            topics_error=topics_error,
            sent=sent,
            received=received,
            templates_error=sent_error or received_error,
        )

    async def _read_agreement(
        self, reader: AnalyticsReader
    ) -> tuple[AgreementView, list[PairView], str]:
        """A1 twice: the verdict, and the ranking the verdict was drawn from.

        Both, because they answer different halves of one question. The
        cross-check says whether the edge may be believed; the listing is what
        the edge actually says, spans and all. A panel showing only a verdict
        would be asking a human to trust a coloured badge over an archive.
        """
        verdict, verdict_error = await _answered(
            lambda: AgreementView.from_agreement(reader.co_addressed_agreement()),
            "the co-addressed cross-check",
            NO_AGREEMENT,
        )
        listed, listing_error = await _answered(
            _projected(reader.top_co_addressed, PairView.from_row),
            "the co-addressed pairs",
            [],
        )
        return verdict, listed, verdict_error or listing_error

    def _follow(self) -> EventCallback[()] | None:
        """Start watching the rebuild, unless this page already is."""
        if self.polling:
            return None
        self.polling = True
        return AnalyticsInsightsState.poll

    async def _adopt_open_rebuild(self) -> None:
        """Take over a rebuild somebody else queued, rather than adding one.

        ``job_id`` is per page and starts at zero, so "is one already running?"
        cannot be answered from this state alone: a second tab, or the same tab
        after a reload, knows about no rebuild and would queue its own. A
        ``derive`` deletes the derived layer before it merges, so two of them
        interleaving can leave a half-built one.

        A queue that cannot answer is not a reason to refuse: the enqueue right
        after would fail too, and that path already has the message for it.
        """
        try:
            open_job = await self._queue().find_open(JobKind.DERIVE)
        except Exception:
            logger.exception("Could not ask the queue for an open rebuild")
            return
        if open_job is None:
            return
        self.job = RebuildJobView.from_job(open_job)
        self.job_id = self.job.job_id
        logger.info("Following rebuild job %d, queued elsewhere", self.job_id)

    def _queue(self) -> JobQueue:
        """Built per call, never at import.

        The session factory it defaults to is configured while the app starts;
        a queue built at module level would capture the world too early.
        """
        return JobQueue()

    async def _read_job(self, job_id: int) -> RebuildJobView | None:
        """The watched job as this panel shows it, or nothing if it is gone."""
        job = await self._queue().get(job_id)
        return None if job is None else RebuildJobView.from_job(job)

    async def _sync_job(self) -> None:
        """One read of the watched job, applied — for everything but the poll.

        A read that fails leaves the last reading standing rather than blanking
        the control: a database hiccup is not news that the rebuild ended.
        """
        if self.job_id <= 0:
            return
        try:
            self.job = await self._read_job(self.job_id) or NO_JOB
        except Exception:
            logger.exception("Could not read rebuild job %d", self.job_id)

    def _reading(self) -> None:
        """Every panel back to "has not answered yet"."""
        self.loading_totals = True
        self.loading_agreement = True
        self.loading_groups = True
        self.loading_topics = True
        self.loading_templates = True

    def _apply(self, readout: Readout) -> None:
        """One refresh's answers, all of them, at once."""
        self.totals = readout.totals
        self.totals_error = readout.totals_error
        self.agreement = readout.agreement
        self.pairs = readout.pairs
        self.agreement_error = readout.agreement_error
        self.groups = readout.groups
        self.groups_error = readout.groups_error
        self.topics = readout.topics
        self.topics_error = readout.topics_error
        self.sent_templates = readout.sent
        self.received_templates = readout.received
        self.templates_error = readout.templates_error
        self.loading_totals = False
        self.loading_agreement = False
        self.loading_groups = False
        self.loading_topics = False
        self.loading_templates = False
