"""Everything the dashboard reaches outside itself for, and nothing else.

The seam :mod:`mailarc_ui.embedder.reads` established: what is here touches the
service registry, the archive's database and the graph, takes no state lock,
touches no var, and is callable from a test without a Reflex state at all. What
is left in :mod:`mailarc_ui.dashboard.state` is the Reflex class — the vars, the
handlers, the lock and the gate.

Six services, all read out of the registry inside the function that needs one.
``mailarc-ui`` may not import ``app`` (§6), so every object the composition root
built arrives that way; a lookup at module level would run while ``app/app.py``
is still being imported, before anything had been published.

**A row never leaves a session.** Reflex serialises what a state holds and a
SQLAlchemy row whose session has closed hands back nothing, so the projections
here run inside the ``async with`` and what comes out is a frozen model.
"""

from appkit_commons.database.session import get_asyncdb_session
from appkit_commons.registry import service_registry

from mailarc_analytics import AnalyticsReader, ArchivedDay, ArchiveTotals
from mailarc_analytics.semantic import SemanticControl, SemanticSearch, VectorCoverage
from mailarc_core.database.entities import SyncJobState
from mailarc_core.database.repositories import (
    FailedMessageRepository,
    MailAccountRepository,
    SyncJobRepository,
)
from mailarc_core.graph import GraphHealth
from mailarc_core.graph.model import GraphServerStatus
from mailarc_core.storage import StorageReader, StorageUsage
from mailarc_ui.dashboard.model import (
    NOTIFICATION_LIMIT,
    DashboardCounts,
    NotificationView,
    VectorState,
    notifications_of,
)

_ACCOUNTS = MailAccountRepository()
_JOBS = SyncJobRepository()
_FAILURES = FailedMessageRepository()
"""Stateless, so one of each is enough for the whole application."""


def analytics_reader() -> AnalyticsReader:
    """The reader the composition root published. Call inside a method only."""
    return _published(AnalyticsReader, "analytics reader")


def semantic_search() -> SemanticSearch:
    return _published(SemanticSearch, "semantic search")


def semantic_control() -> SemanticControl:
    return _published(SemanticControl, "embedder control")


def storage_reader() -> StorageReader:
    return _published(StorageReader, "storage reader")


def graph_health() -> GraphHealth:
    return _published(GraphHealth, "graph health")


def archive_reading() -> tuple[ArchiveTotals, VectorCoverage]:
    """The archive's six counts and its embedding coverage, in one thread hop.

    Together because they come off the same graph and fail together, and
    because they feed one panel between them: the "Archived Emails" tile is the
    message count and the health card divides three pairs out of both.

    Blocking — every runic driver is — so the caller puts it in a thread.
    """
    return analytics_reader().totals(), semantic_search().coverage()


def archived_days(days: int) -> tuple[ArchivedDay, ...]:
    """How the archive grew, one row per day, oldest first.

    The one statement §1.3 puts both charts *and* the "last archived" tile
    behind. Deliberately not paired with a second read over
    ``mail_archived_messages`` in SQLite: two sources for one number is a bug
    waiting for its appointment.
    """
    return analytics_reader().archived_per_day(days=days)


def disk_usage() -> StorageUsage:
    """What the archive occupies, path by path. Blocking: it walks trees."""
    return storage_reader().usage()


def vector_state() -> VectorState:
    """Whether an embedder is configured, and whether the index agrees with it."""
    search = semantic_search()
    return VectorState(
        configured=search.available,
        index=search.index_dimension(),
        dimension=semantic_control().current().dimension,
    )


async def graph_status() -> GraphServerStatus:
    """A fresh snapshot of the graph server.

    Never raises on an outage: an unreachable server comes back as a status
    with ``reachable`` false, which is a row the checklist renders like any
    other.
    """
    return await graph_health().status()


async def database_counts() -> DashboardCounts:
    """Accounts, users and the job queue — one session, three questions.

    The queue is counted by the database rather than by loading its rows: a
    long-lived archive has tens of thousands of succeeded jobs behind the one
    number nobody looks at.
    """
    from appkit_user.authentication.backend.database import user_repo

    async with get_asyncdb_session() as session:
        accounts = await _ACCOUNTS.count(session)
        states = await _JOBS.count_by_state(session)
        users = await user_repo.count(session)
    return DashboardCounts(
        accounts=accounts,
        users=users,
        queued=states.get(SyncJobState.QUEUED, 0),
        running=states.get(SyncJobState.RUNNING, 0),
    )


async def pending_notifications(
    *, limit: int = NOTIFICATION_LIMIT
) -> list[NotificationView]:
    """Everything currently wrong, as the panel prints it.

    **Administrators only** — the caller is what enforces that, and this
    function is never reached for anybody else. Three reads and one projection,
    all inside one session, because two of the three answer with rows whose
    attributes are gone once it closes.

    All three reads are bounded. The failed jobs used to come from
    ``find_by_state``, which has no ``LIMIT`` and orders oldest-first, so this
    page loaded every job the archive ever failed in order to print at most
    eight lines of them — and it does so on a page a signed-out visitor can
    open. :meth:`~mailarc_core.database.repositories.SyncJobRepository.find_recent_failed`
    is the finder that ends that.
    """
    async with get_asyncdb_session() as session:
        failures = await _FAILURES.find_recent(session, limit=limit)
        accounts = await _ACCOUNTS.find_all(session)
        jobs = await _JOBS.find_recent_failed(session, limit=limit)
        return notifications_of(failures, accounts, jobs, limit=limit)


def _published[T](wanted: type[T], what: str) -> T:
    """One service, or the sentence that names the call somebody missed.

    ``ServiceRegistry.get`` raises a bare ``KeyError`` naming a registry
    nobody looking at a dashboard has heard of. The panel that asked shows
    whatever comes back from here, so it says what is actually missing.
    """
    try:
        return service_registry().get(wanted)
    except KeyError as error:
        raise RuntimeError(
            f"No {what} is registered — did app.composition run?"
        ) from error
