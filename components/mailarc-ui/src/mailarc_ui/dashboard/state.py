"""The dashboard's six panels, and the line drawn across the middle of them.

Modelled on :class:`~mailarc_ui.insights.state.AnalyticsInsightsState` without
deviation: every collaborator is looked up inside the method that needs it and
never at import, every blocking read goes to a thread, and **every panel
carries its own loading flag and its own error string** with the read and its
projection inside one guard. That last point is not symmetry for its own sake —
a projection renders dates, and a ``Date:`` header is whatever a sender wrote.
One archived mail from the year 9999 raised ``OverflowError`` out of every
listing on the insights page at once and left it spinning with no error text
and no way back.

Where this state does something the insights page does not is authorisation,
and it is a **deliberate departure from the specification** that belongs in
writing.

§7 asks for a public dashboard on ``/``, and that is what this is: no sign-in,
no admin gate, :func:`~mailarc_ui.shell.templates.public_page` rather than
``authenticated_page``. But "the page is public" cannot mean "everything on it
is public", because two of the six panels are not summaries of an archive at
all — they are extracts from it:

* **Notifications** carry mailbox addresses, the ``last_error`` a provider
  wrote, and the ``detail`` of a message the import gave up on. That is
  per-person data out of everybody's private mail, in sentences.
* **The disk panel's paths** say where an installation keeps that mail, which
  is the first thing worth knowing to go and take it.

So the line is drawn here, at the point where data leaves the process, and the
split is:

* **Public** — the KPI counts, both charts, the archive-health ratios, and the
  services checklist **as booleans only**: no endpoint, host, port, path or
  version string reaches it, which is why
  :func:`~mailarc_ui.dashboard.model.services_of` takes a status object and
  answers with five names and five bools.
* **Administrator** — the notifications panel in full, and the absolute paths
  in the disk panel. A visitor keeps that panel's labels, ratios and
  percentages and loses only the paths; and sees notifications as a neutral
  empty state rather than as an error or as a count, because a count of faults
  is itself a fact about the installation.

The gate has to sit *here* and not on the decorator, and this is the part that
is easy to get wrong: ``admin_only`` is a render-time ``rx.cond``, and appkit's
``_build_auth_handlers`` runs the rest of an ``on_load`` chain whatever
``check_auth`` returned. A handler that ships data therefore runs for a
logged-out visitor, and has to refuse for itself. :meth:`_may_see_private` is
the same shape as
:meth:`~mailarc_ui.insights.state.AnalyticsInsightsState._may_read`, including
**fails closed**: a process where the asker cannot be established is a process
that cannot call them an administrator.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import reflex as rx

from mailarc_ui.dashboard import reads
from mailarc_ui.dashboard.model import (
    DEFAULT_RANGE,
    UNKNOWN,
    DashboardCounts,
    MeterView,
    NotificationView,
    Readout,
    ServiceView,
    VectorState,
    chosen_range,
    days_in,
    health_meters,
    last_archived_label,
    messages_points,
    services_of,
    storage_meters,
    storage_points,
    thousands,
)
from mailarc_ui.shell.access import granted, signed_in_user

logger = logging.getLogger(__name__)

_SERVICES_TRAILING = 2
"""How many rows sit below the checklist's one divider.

The last two are the embedder's — whether one is configured, and whether the
live index agrees with it — and the three above are the graph server's. Two
groups, one divider, which is what the design draws.
"""


def _refusal(what: str) -> str:
    """What a panel prints when its read failed — written here, never quoted.

    **The exception text does not cross to the browser.** ``/`` is public, and
    the exceptions these reads actually raise name the installation: an
    unreachable graph is ``ConnectionError: Error 61 connecting to
    127.0.0.1:6379``, and an unreadable mailstore is a ``PermissionError``
    carrying the absolute path. Those are the two facts
    :func:`~mailarc_ui.dashboard.components.services_card` refuses to print for
    a reason, and putting them on a var because a read failed would be the same
    disclosure through a side door.

    Still per-panel and still specific, because the point of an error string is
    that a reader can tell a quiet archive from a broken one. What is lost is
    the detail, and ``logger.exception`` has kept every word of it.
    """
    return f"Could not read {what}."


async def _answered[T](work: Callable[[], T], what: str, empty: T) -> tuple[T, str]:
    """One blocking read *and* its projection, off the loop, failure and all.

    Every runic driver blocks and the storage reader walks directory trees, so
    the work goes to a thread. What comes back from a failure is a sentence for
    the panel that asked rather than an exception: a graph that went away is a
    state this page has to render, and one dead panel must not take the other
    five with it. Which sentence, and why not the exception's own, is
    :func:`_refusal`.
    """
    try:
        return await asyncio.to_thread(work), ""
    except Exception:
        logger.exception("Could not read %s", what)
        return empty, _refusal(what)


async def _awaited[T](
    work: Callable[[], Awaitable[T]], what: str, empty: T
) -> tuple[T, str]:
    """The same contract for work that is already asynchronous.

    Two functions rather than one that inspects what it was handed: the
    database reads are coroutines and the graph reads are blocking calls, and a
    helper that guessed would put a coroutine into a thread on the day somebody
    changed one of them.
    """
    try:
        return await work(), ""
    except Exception:
        logger.exception("Could not read %s", what)
        return empty, _refusal(what)


class DashboardState(rx.State):
    """What the archive looks like at a glance, for whoever is looking.

    Six panels, six loading flags, five error strings — the services checklist
    is the sixth panel and says a failed read by going grey. The three string
    tiles start at :data:`~mailarc_ui.dashboard.model.UNKNOWN` rather than at
    nought, because "nobody has answered yet" is not "the archive is empty".
    """

    range: str = DEFAULT_RANGE
    """Which window both charts draw — ``week``, ``month`` or ``year``.

    One var and one read for two charts: they plot two fields of the same
    statement, and a page that asked twice could get two different moments of
    the same archive.
    """

    archived: str = UNKNOWN
    accounts: str = UNKNOWN
    queued: str = UNKNOWN
    users: str = UNKNOWN
    last_archived: str = UNKNOWN

    health: list[MeterView] = []
    storage: list[MeterView] = []
    notifications: list[NotificationView] = []
    services: list[ServiceView] = []

    messages_series: list[dict[str, Any]] = []
    storage_series: list[dict[str, Any]] = []
    """The two chart series, in the record shape ``mn.area_chart`` declares.

    Plain dicts and not value objects because ``data`` on every
    ``appkit_mantine`` chart is ``Var[list[dict[str, Any]]]`` — a model handed
    to it is a prop Reflex refuses at build time. The shape is built once, in
    :mod:`mailarc_ui.dashboard.model`, so no component assembles records.
    """

    archive_error: str = ""
    counts_error: str = ""
    series_error: str = ""
    storage_error: str = ""
    notifications_error: str = ""
    """Five error strings for six panels — the checklist has none, and
    :attr:`~mailarc_ui.dashboard.model.Readout.services` says why."""

    loading_archive: bool = True
    loading_counts: bool = True
    loading_series: bool = True
    loading_storage: bool = True
    loading_notifications: bool = True
    loading_services: bool = True
    """True to begin with, one per panel.

    The page renders before its first read returns, and an empty card is a
    claim about the archive that nothing has earned yet. All six are cleared by
    the same :meth:`_apply`, so a panel cannot be left spinning over data that
    arrived.
    """

    @rx.var
    def services_split(self) -> int:
        """Where the checklist's dotted divider goes — before the last group.

        The design draws one divider, separating the last two rows from the
        rest, and the state is what decides where: a component counting rows
        inside an ``rx.foreach`` would be arithmetic on a ``Var``, and a flag on
        :class:`~mailarc_ui.dashboard.model.ServiceView` would put a rule about
        drawing into a value object. Nought while the list is empty, which is
        an index no row has.
        """
        return max(len(self.services) - _SERVICES_TRAILING, 0)

    @rx.var
    def has_notifications(self) -> bool:
        """Whether the panel has anything to list.

        False is the ordinary state of a healthy archive *and* the state a
        visitor is shown, and both render the same empty card on purpose — a
        refusal that looked different from health would tell an anonymous
        caller that there is something they are not being shown.
        """
        return len(self.notifications) > 0

    @rx.event(background=True)
    async def load(self) -> None:
        """Read every panel. The page's ``on_load``.

        A background task, so the state lock is held around the two mutations
        and never around a read: the archive-wide counts and the disk walk are
        the two slowest things on the page, and a plain handler would hold this
        client's lock for the whole of both.
        """
        async with self:
            self._reading()
            allowed = await self._may_see_private()
            chosen = self.range
        readout = Readout()
        try:
            readout = await self._read_everything(chosen, allowed=allowed)
        finally:
            # In a `finally` because the alternative is six panels spinning for
            # ever over a page that will never say why. Every read below has its
            # own guard, so reaching here with an exception means something
            # outside one threw — and an empty `Readout` renders as an archive
            # that answered nothing, which is at least a state a reader can see.
            async with self:
                self._apply(readout)

    @rx.event(background=True)
    async def choose_range(self, value: str) -> None:
        """Redraw both charts over a different window, from one read.

        Only the series panel moves. The counts, the disk figures and the
        checklist do not depend on the window, and re-reading them would make
        a switch between two chart widths cost a walk of the mailstore.
        """
        async with self:
            self.range = chosen_range(value)
            self.loading_series = True
            self.series_error = ""
            chosen = self.range
        found = await self._read_series(chosen)
        async with self:
            self._apply_series(found)

    async def _read_everything(self, chosen: str, *, allowed: bool) -> Readout:
        """Ask each panel's question once, and let each one fail on its own."""
        archived, health, archive_error = await self._read_archive()
        counts, counts_error = await _awaited(
            reads.database_counts, "the archive's own counts", DashboardCounts()
        )
        series = await self._read_series(chosen)
        storage, storage_error = await _answered(
            lambda: storage_meters(reads.disk_usage(), with_paths=allowed),
            "what the archive occupies on disk",
            [],
        )
        notifications, notifications_error = await self._read_notifications(
            allowed=allowed
        )
        services = await self._read_services(None if counts_error else counts)
        return Readout(
            archived=archived,
            health=health,
            archive_error=archive_error,
            accounts=UNKNOWN if counts_error else thousands(counts.accounts),
            queued=UNKNOWN if counts_error else thousands(counts.queued),
            users=UNKNOWN if counts_error else thousands(counts.users),
            counts_error=counts_error,
            last_archived=series.last_archived,
            messages_series=series.messages_series,
            storage_series=series.storage_series,
            series_error=series.series_error,
            storage=storage,
            storage_error=storage_error,
            notifications=notifications,
            notifications_error=notifications_error,
            services=services,
        )

    async def _read_archive(self) -> tuple[str, list[MeterView], str]:
        """The message count and the health ratios — one graph, one guard."""

        def work() -> tuple[str, list[MeterView]]:
            totals, coverage = reads.archive_reading()
            return thousands(totals.messages), health_meters(totals, coverage)

        (archived, health), error = await _answered(
            work, "the archive totals", (UNKNOWN, [])
        )
        return archived, health, error

    async def _read_series(self, chosen: str) -> Readout:
        """Both chart series and the "last archived" tile, from one statement.

        §1.3 in one method: the tile is the newest day of this series and not a
        second read over SQLite, so a graph that is down takes all three with it
        and the services card underneath says why. Two sources for one number
        is a bug waiting for its appointment.
        """

        def work() -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
            days = reads.archived_days(days_in(chosen))
            return (
                last_archived_label(days),
                messages_points(days),
                storage_points(days),
            )

        (last, messages, storage), error = await _answered(
            work, "the archiving history", (UNKNOWN, [], [])
        )
        return Readout(
            last_archived=last,
            messages_series=messages,
            storage_series=storage,
            series_error=error,
        )

    async def _read_notifications(
        self, *, allowed: bool
    ) -> tuple[list[NotificationView], str]:
        """The pending faults — for an administrator, and for nobody else.

        A refusal answers with an empty list and an **empty error**, which is
        the same thing a healthy archive answers with. That is deliberate: an
        error, or a count of what is being withheld, would tell an anonymous
        caller how many things are wrong with this installation, and the count
        is the fact worth having.

        The read is not made at all rather than made and filtered. Nothing that
        cannot be shown is fetched, so there is no branch downstream where it
        could be shown by accident.
        """
        if not allowed:
            return [], ""
        return await _awaited(reads.pending_notifications, "the pending failures", [])

    async def _read_services(self, counts: DashboardCounts | None) -> list[ServiceView]:
        """Whether the machinery is up, as five names and five booleans.

        The status object and the two vector lengths stay in this method:
        :func:`~mailarc_ui.dashboard.model.services_of` is what turns them into
        rows, and what it answers with carries no endpoint, no version and no
        length. This card is public.

        **This is the one panel with no error string, and that is the point.**
        A read that failed reaches ``services_of`` as ``None``, which is a
        checklist that says the thing is down — which is what a failed read
        means and what §1.3 promises this card will show when the graph tiles
        go to an em dash. An alert in its place would blank out the checklist
        in exactly the case it was written for. ``reads.vector_state`` is not
        hypothetical here: it opens a graph session, so an unreachable server
        raises out of it rather than answering.
        """
        status, _ = await _awaited(reads.graph_status, "the graph server", None)
        vectors, vectors_error = await _answered(
            reads.vector_state, "the vector index", VectorState()
        )
        return services_of(status, None if vectors_error else vectors, counts)

    async def _may_see_private(self) -> bool:
        """Whether this session may be shown the two administrator panels.

        Not the page's gate — ``/`` has none, and is meant not to. This is the
        gate on the *data*, and it is the only one there is: appkit runs an
        ``on_load`` chain to the end whatever ``check_auth`` returned, so
        :meth:`load` runs for every visitor, and a handler that ships mailbox
        addresses has to decide for itself.

        Refused at ``DEBUG`` rather than at ``WARNING``, unlike every other gate
        in this package: a visitor arriving at a public page and being served
        the public half of it is the ordinary working of this page, not an
        incident. Fails closed all the same — see
        :func:`mailarc_ui.shell.access.granted`.
        """
        return await granted(
            self._current_user,
            what="the private half of the dashboard",
            level=logging.DEBUG,
        )

    async def _current_user(self) -> object | None:  # pragma: no cover
        """The signed-in user of this session, or ``None``.

        Its own method so :meth:`_may_see_private` can be tested without a
        running Reflex app; see
        :func:`mailarc_ui.shell.access.signed_in_user`.
        """
        return await signed_in_user(self)

    def _reading(self) -> None:
        """Every panel back to "has not answered yet"."""
        self.loading_archive = True
        self.loading_counts = True
        self.loading_series = True
        self.loading_storage = True
        self.loading_notifications = True
        self.loading_services = True

    def _apply(self, readout: Readout) -> None:
        """One refresh's answers, all of them, at once."""
        self.archived = readout.archived
        self.health = readout.health
        self.archive_error = readout.archive_error
        self.accounts = readout.accounts
        self.queued = readout.queued
        self.users = readout.users
        self.counts_error = readout.counts_error
        self.storage = readout.storage
        self.storage_error = readout.storage_error
        self.notifications = readout.notifications
        self.notifications_error = readout.notifications_error
        self.services = readout.services
        self._apply_series(readout)
        self.loading_archive = False
        self.loading_counts = False
        self.loading_storage = False
        self.loading_notifications = False
        self.loading_services = False

    def _apply_series(self, readout: Readout) -> None:
        """The chart panel's three answers — the half the range switch redraws."""
        self.last_archived = readout.last_archived
        self.messages_series = readout.messages_series
        self.storage_series = readout.storage_series
        self.series_error = readout.series_error
        self.loading_series = False
