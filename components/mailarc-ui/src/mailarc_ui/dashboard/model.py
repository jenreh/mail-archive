"""What the dashboard shows, and how every number on it is spelled.

No I/O, no registry, no event loop — a value in, the string a panel prints out.
The split off :mod:`mailarc_ui.dashboard.state` is the one
:mod:`mailarc_ui.insights.model` already makes and for the same reason: a
projection is wrong when a size reads as ``3221225472`` or a stamp lands in the
wrong zone, and a state is wrong when a lock is held across an await or a panel
is left spinning over data that arrived. Neither mistake needs the other's
machinery to be found.

Two rules run through the whole module.

**An unknown value is an em dash, never a nought.** ``0`` is a measurement and
says the archive is empty; ``—`` says nobody could ask. The dashboard's own
specification turns on the difference — the graph feeds the "last archived"
tile and both charts, and when it is down those three have to say so while the
rest of the band keeps its numbers.

**A ratio is measured or it is not shown.** Every bar on this page divides two
numbers that were actually read. Nothing here invents a percentage to make a
card look finished.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict

from mailarc_analytics import ArchivedDay, ArchiveTotals
from mailarc_analytics.semantic import VectorCoverage
from mailarc_core.database.entities import (
    MailAccountEntity,
    MailFailedMessageEntity,
    MailSyncJobEntity,
)
from mailarc_core.graph.model import GraphServerStatus
from mailarc_core.storage import StorageUsage

UNKNOWN = "—"
"""What a tile shows for a number nobody could read.

An em dash and not ``0``, and not an empty cell: an empty cell reads as a
rendering fault, and a nought is a claim about the archive that a failed read
has not earned.
"""

WEEK = "week"
MONTH = "month"
YEAR = "year"

RANGE_DAYS: Mapping[str, int] = MappingProxyType({WEEK: 7, MONTH: 30, YEAR: 365})
"""How wide each of the three chart ranges is, in days.

A closed table rather than an arithmetic on a label, because the value arrives
from a browser over a socket and :func:`days_in` is the only thing between it
and the row ceiling a graph read is given.
"""

DEFAULT_RANGE = MONTH
"""What the page opens on.

A month rather than the first entry in the switch: a week of an archive whose
mailboxes are synced weekly is one column and five empty ones, which reads as a
broken chart rather than as a quiet week.
"""

NOTIFICATION_LIMIT = 8
"""How many pending faults the panel lists.

The panel reads faults, it does not manage them (§13), so the list is a sample
worth acting on rather than a ledger. Everything below the cut is still in the
accounts page, the job queue and the failure table it was read from.
"""

_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
_BYTE_STEP = 1024.0
_PERCENT = 100.0


def days_in(value: str) -> int:
    """How many days the named range covers; the default for anything else.

    Falls back rather than raising. ``choose_range`` is reachable by name over
    the socket, so "week" is a suggestion from the client and not a promise,
    and a page that raised on a typo would answer a malformed event with a
    stack trace instead of a chart.
    """
    return RANGE_DAYS.get(value, RANGE_DAYS[DEFAULT_RANGE])


def chosen_range(value: str) -> str:
    """The range to remember for a value a client sent.

    Anything the table does not name becomes the default, so the segmented
    control can never end up bound to a value it does not offer — which renders
    as a switch with nothing selected and no way to get back.
    """
    return value if value in RANGE_DAYS else DEFAULT_RANGE


def thousands(value: int) -> str:
    """A count as a reader groups it: ``12,400``."""
    return f"{value:,}"


def human_bytes(value: int) -> str:
    """A size as the reference prints it: ``3.0 GB``.

    Binary steps under decimal names, which is what every file manager on the
    three platforms this ships to does — the alternative is a mailstore that
    reads as 3.2 GB here and 3.0 GB in Finder beside it.

    Whole bytes stay whole: ``512 B`` and not ``512.0 B``, because a decimal on
    a number that cannot have one reads as a rounding that happened.
    """
    if value <= 0:
        return "0 B"
    size = float(value)
    unit = 0
    while size >= _BYTE_STEP and unit < len(_BYTE_UNITS) - 1:
        size /= _BYTE_STEP
        unit += 1
    if unit == 0:
        return f"{int(size)} B"
    return f"{size:.1f} {_BYTE_UNITS[unit]}"


def gigabytes(value: int) -> float:
    """*value* as the number a chart axis labelled GB plots."""
    return round(value / _BYTE_STEP**3, 2)


def percent_label(value: float) -> str:
    """A ratio already scaled to ``0..100``, as a whole percentage."""
    return f"{round(value)}%"


def ratio_percent(part: int, whole: int) -> float:
    """*part* of *whole* on the ``0..100`` scale a progress bar takes.

    Nought when there is nothing to divide by, rather than a
    ``ZeroDivisionError``: an archive that holds no messages has no coverage,
    and that is a bar at nought and not a broken page. Capped, because two
    counts read from different statements can disagree by one at the moment a
    sync commits, and a bar at 103 % renders as a bar that overflows its track.
    """
    if whole <= 0:
        return 0.0
    return min(_PERCENT, _PERCENT * part / whole)


def as_aware(value: datetime) -> datetime:
    """A stamp with a zone on it, reading a naive one as UTC.

    Every timestamp this archive writes is UTC — the archiver stamps
    ``datetime.now(UTC)`` and the job queue does the same — but SQLite hands
    back naive datetimes for a ``DateTime(timezone=True)`` column. Read as
    local time, every one of them would silently move by the reader's offset.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def moment_label(value: datetime | None) -> str:
    """A timestamp as the reference prints it: ``Aug 7, 2025. 23:01``.

    In the reader's own zone, because a notification says when something went
    wrong for the person reading it. Built by hand rather than with ``%-d``,
    which is a platform extension and not the same flag on every libc this
    ships to.
    """
    if value is None:
        return UNKNOWN
    local = as_aware(value).astimezone()
    return f"{local:%b} {local.day}, {local.year}. {local:%H:%M}"


def day_label(value: str) -> str:
    """One ``YYYY-MM-DD`` key as ``Aug 7, 2025``; the em dash for anything else.

    Unreadable keys are real: the day comes out of ``left(archived_at, 10)``,
    which cuts ten characters off whatever the property holds and never fails,
    so an edge stamped by something other than the archiver arrives here as a
    key no calendar has.
    """
    try:
        found = date.fromisoformat(value)
    except ValueError:
        return UNKNOWN
    return f"{found:%b} {found.day}, {found.year}"


class MeterView(BaseModel):
    """One labelled bar: what it measures, how full it is, and what that is.

    Both statistics cards are rows of these — archive health on one, disk usage
    on the other — because they are the same thing measured over different
    populations, and a second model would let the two drift apart in a design
    that draws them identically.
    """

    model_config = ConfigDict(frozen=True)

    icon: str
    label: str
    percent: float
    """Already on the ``0..100`` scale ``mn.progress`` takes."""

    value: str
    """The percentage as the row prints it, on the right."""

    caption: str = ""
    """The dimmed size beside the label — ``3.0 GB / 7.5 GB``. Empty where the
    ratio is the whole story, which is every row of the health card."""

    detail: str = ""
    """The absolute path this row measured. **Administrators only.**

    Empty for everybody else, and empty is what it means: a filesystem layout
    says where an installation keeps somebody's mail, which is the first thing
    an attacker asks a public page for.
    """


class NotificationView(BaseModel):
    """One thing that needs somebody's attention, and when it happened.

    **Administrators only, in full.** Every one of these carries a mailbox
    address, an error a provider wrote, or the detail of a message the import
    gave up on — per-person data out of everybody's private mail. What a
    visitor gets instead is an empty panel, which is the honest rendering of a
    question they are not allowed to ask.
    """

    model_config = ConfigDict(frozen=True)

    message: str
    when: str


class ServiceView(BaseModel):
    """One row of the services checklist: a name, and whether it is up.

    **A boolean and nothing else** — no endpoint, no host, no port, no version.
    This card is public, and "FalkorDB is reachable" tells a visitor the
    archive is working while "FalkorDB 4.0.9 at localhost:6379" tells them what
    to attack. The graph status page is where the facts live, and it is
    administration.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    up: bool


class VectorState(BaseModel):
    """What the embedder and the live vector index say about each other.

    Never crosses to the browser: the services card renders one boolean drawn
    from it, and the two lengths themselves are exactly the installation detail
    that card must not print.
    """

    model_config = ConfigDict(frozen=True)

    configured: bool = False
    """Whether an embedder exists at all — ``provider: none`` is the default."""

    index: int = 0
    """Floats the graph's vector index will actually keep, or nought for none."""

    dimension: int = 0
    """Floats the configured embedder would write."""

    @property
    def dimension_matches(self) -> bool:
        """Whether a vector this archive writes is one the index will keep.

        Against the **live index** and not against the configuration alone, for
        the reason ``SemanticSearch.index_dimension`` gives: the configuration
        is exactly what can be wrong, and a vector of the wrong length is
        stored, never indexed, and reported nowhere.
        """
        return self.index > 0 and self.index == self.dimension


class DashboardCounts(BaseModel):
    """The three numbers the archive's own database answers.

    Read in one session and carried together because they are one panel's
    worth: all three are tiles in the KPI band, and the last one also decides
    whether anything is serving the queue.
    """

    model_config = ConfigDict(frozen=True)

    accounts: int = 0
    queued: int = 0
    running: int = 0


class Readout(BaseModel):
    """Everything one refresh read, before any of it is a state var.

    The shape :class:`mailarc_ui.insights.model.Readout` established, for the
    same reason: ``load`` is a background task, so the state lock goes around
    the assignment and around nothing that awaits. It also means the six panels
    are always showing one moment of the archive rather than a mixture of
    before and after.
    """

    model_config = ConfigDict(frozen=True)

    archived: str = UNKNOWN
    health: list[MeterView] = []
    archive_error: str = ""

    accounts: str = UNKNOWN
    queued: str = UNKNOWN
    running: str = UNKNOWN
    counts_error: str = ""

    last_archived: str = UNKNOWN
    messages_series: list[dict[str, Any]] = []
    storage_series: list[dict[str, Any]] = []
    series_error: str = ""

    storage: list[MeterView] = []
    storage_error: str = ""

    notifications: list[NotificationView] = []
    notifications_error: str = ""

    services: list[ServiceView] = []
    """No error string beside it, unlike the other five panels.

    A checklist row already has a way to say "could not ask" — it goes grey —
    so a failed read reaches :func:`services_of` as a ``None`` and renders as
    the thing being down. That is what §1.3 promises this card will show when
    the graph tiles go to an em dash, and an alert in its place would blank the
    checklist out in exactly the case it was written for."""


def messages_points(days: Sequence[ArchivedDay]) -> list[dict[str, Any]]:
    """The archived-per-day series as Mantine's chart wants it.

    A list of plain records and not a list of models, deliberately: ``data`` on
    every ``appkit_mantine`` chart is declared ``Var[list[dict[str, Any]]]``,
    and a value object handed to it is a prop Reflex refuses at build time.
    This is a wire format for one component, which is exactly why it is built
    here — one place that knows it, rather than a component assembling records
    out of a state var.
    """
    return [{"day": day_label(one.day), "messages": one.messages} for one in days]


def storage_points(days: Sequence[ArchivedDay]) -> list[dict[str, Any]]:
    """The same series in gigabytes, for the chart whose axis reads in GB.

    Its own list rather than one series carrying both numbers: a record holding
    a count and a size sends both to the browser twice over, and the two charts
    never draw the same field.
    """
    return [
        {"day": day_label(one.day), "storage": gigabytes(one.bytes)} for one in days
    ]


def last_archived_label(days: Sequence[ArchivedDay]) -> str:
    """The newest day the archive actually copied something, as a label.

    The em dash when the series is empty or every day of it is quiet — which is
    what "nothing has been archived yet" looks like, and is not the same
    statement as a day with a nought on it.
    """
    for one in reversed(days):
        if one.messages > 0:
            return day_label(one.day)
    return UNKNOWN


def health_meters(totals: ArchiveTotals, coverage: VectorCoverage) -> list[MeterView]:
    """Archive health as three ratios that were measured.

    Not three invented percentages. Embedding coverage is vectors over
    messages, identified senders is the complement of the archive's own
    ``unidentified`` count, and the third is a presence: a derived layer either
    exists or it does not, so its bar is full or empty and the number under it
    says which. Rendering "the analysis has been run" as a proportion of
    anything would be the fake percentage this card is written to avoid.
    """
    identified = totals.messages - totals.unidentified
    derived = _PERCENT if totals.derived > 0 else 0.0
    return [
        _meter(
            "brain",
            "Embedded messages",
            ratio_percent(coverage.embedded, coverage.total),
        ),
        _meter(
            "user-check",
            "Identified senders",
            ratio_percent(identified, totals.messages),
        ),
        _meter("layers", "Derived layer", derived),
    ]


def storage_meters(usage: StorageUsage) -> list[MeterView]:
    """One bar per measured path, of how much of its volume it takes."""
    return [
        MeterView(
            icon=icon,
            label=one.label,
            percent=one.used_percent,
            value=percent_label(one.used_percent),
            caption=f"{human_bytes(one.used_bytes)} / {human_bytes(one.total_bytes)}",
            detail=str(one.path),
        )
        for icon, one in zip(_DISK_ICONS, usage.paths, strict=False)
    ]


def services_of(
    status: GraphServerStatus | None,
    vectors: VectorState | None,
    counts: DashboardCounts | None,
) -> list[ServiceView]:
    """The five rows of the checklist, as booleans and nothing else.

    Every argument is optional and ``None`` means "could not ask", which reads
    as down. That is the honest rendering on a card whose whole job is to say
    whether the machinery is working: a row that stayed green because the
    question failed would be worse than one that went grey.

    **"Sync worker running" is an inference and it is worth naming.** Nothing
    in this archive records a worker's heartbeat while it is idle — the only
    heartbeat there is belongs to a claimed job — so an idle worker and a
    missing one are indistinguishable from the outside. What *is* observable is
    the symptom this row exists to catch: work sitting in the queue with
    nothing claiming it. So the row is true while something is running or there
    is nothing to run, and false exactly when jobs are waiting and no worker
    has taken one.
    """
    serving = counts is not None and (counts.running > 0 or counts.queued == 0)
    return [
        ServiceView(
            name="Graph server reachable", up=bool(status and status.reachable)
        ),
        ServiceView(
            name="Vector search supported",
            up=bool(status and status.vector_knn_supported),
        ),
        ServiceView(name="Sync worker running", up=serving),
        ServiceView(
            name="Embedder configured", up=bool(vectors and vectors.configured)
        ),
        ServiceView(
            name="Vector index dimension matches",
            up=bool(vectors and vectors.dimension_matches),
        ),
    ]


def notifications_of(
    failures: Sequence[MailFailedMessageEntity],
    accounts: Sequence[MailAccountEntity],
    jobs: Sequence[MailSyncJobEntity],
    *,
    limit: int = NOTIFICATION_LIMIT,
) -> list[NotificationView]:
    """Everything currently wrong, newest first, capped.

    Three sources because there are three ways this archive fails and a reader
    acting on one of them does not care which table it came from: a mailbox
    that will not authenticate, a job that ended badly, and a message the
    import gave up on. A failure with no stamp on it sorts last rather than
    being dropped — an unrecorded time is not a reason to stop reporting a
    fault.

    Entities in, views out, and nothing here opens a session: the caller reads
    inside one and hands the rows over, which is what keeps this module free of
    I/O and testable with three lists.
    """
    addresses = {one.id: one.email_address for one in accounts}
    pending: list[tuple[datetime | None, str]] = [
        *(
            (
                one.last_sync_at,
                f"{one.email_address} — {one.last_error or 'the last sync failed'}",
            )
            for one in accounts
            if one.status in _BROKEN
        ),
        *(
            (
                one.finished_at,
                f"A {one.kind} job failed — {one.error or 'no reason was recorded'}",
            )
            for one in jobs
        ),
        *(
            (
                one.occurred_at,
                f"{addresses.get(one.account_id, 'A mailbox')}: {one.reason}"
                f"{f' — {one.detail}' if one.detail else ''}",
            )
            for one in failures
        ),
    ]
    pending.sort(key=lambda one: as_aware(one[0]) if one[0] else _NEVER, reverse=True)
    return [
        NotificationView(message=message, when=moment_label(when))
        for when, message in pending[:limit]
    ]


def _meter(icon: str, label: str, percent: float) -> MeterView:
    """One health row: the ratio is the whole story, so there is no caption."""
    return MeterView(
        icon=icon, label=label, percent=percent, value=percent_label(percent)
    )


_DISK_ICONS = ("mails", "database", "hard-drive")
"""One icon per measured path, in the order the composition root measures them.

Zipped rather than indexed, and zipped without ``strict``: the reader is
constructed with the paths it was given, and a fourth one added there should
cost an icon rather than a broken page.
"""

_BROKEN = frozenset({"auth_error", "error"})
"""Account statuses that need a human.

The two strings rather than the enum members, because ``status`` is a plain
``String`` column: a row written by an older build carries whatever it carried,
and a comparison against an enum would quietly match nothing.
"""

_NEVER = datetime.min.replace(tzinfo=UTC)
"""Where a fault with no timestamp sorts: last, and still on the list."""
