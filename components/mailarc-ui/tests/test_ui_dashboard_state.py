"""What the dashboard reads, what it refuses to hand over, and how it prints it.

Three groups of claims, and the middle one is the reason this file is long.

**It reads.** Every panel fills from the source §7.3 names, and one source that
throws sets *that* panel's error while the other five keep their numbers. The
range switch reshapes both series out of one read, because both charts and the
"last archived" tile come from one statement (§1.3).

**It refuses.** ``/`` is public, and appkit runs the whole ``on_load`` chain
whatever ``check_auth`` returned — so the handler below runs for a signed-out
visitor. Half of what it reads is per-person data out of everybody's private
mail, and the tests here are what pin that half to an administrator. The
anonymous case is exercised through the failure that actually happens outside a
request — ``_current_user`` raising — because that is the branch that has to
fail closed rather than fall open.

**It prints.** Bytes, timestamps, counts and the em dash are pure functions
over a value, tested without a graph, a registry or an event loop.
"""

import contextlib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from appkit_user.authentication.backend.database import UserEntity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from who_is_asking import FakeUser, nobody_can_be_established, signed_in_as

from mailarc_analytics import AnalyticsReader, ArchivedDay, ArchiveTotals
from mailarc_analytics.semantic import (
    SemanticConfig,
    SemanticControl,
    SemanticSearch,
    VectorCoverage,
)
from mailarc_core.database.entities import (
    AccountStatus,
    MailAccountEntity,
    MailFailedMessageEntity,
    MailSyncJobEntity,
    SyncJobKind,
    SyncJobState,
)
from mailarc_core.graph import GraphHealth
from mailarc_core.graph.model import GraphServerMode, GraphServerStatus
from mailarc_core.storage import PathUsage, StorageReader, StorageUsage
from mailarc_ui.dashboard import DashboardState
from mailarc_ui.dashboard.model import (
    UNKNOWN,
    DashboardCounts,
    NotificationView,
    Readout,
    ServiceView,
    day_label,
    days_in,
    human_bytes,
    last_archived_label,
    moment_label,
    notifications_of,
    percent_label,
    ratio_percent,
    services_of,
    thousands,
)

READS = "mailarc_ui.dashboard.reads"

NOW = datetime(2025, 8, 7, 23, 1, tzinfo=UTC)
"""The moment the reference screenshot's "Last Archived" tile prints."""

GIGABYTE = 1024**3


class StubReader:
    """The graph half of the dashboard, scripted.

    Registered under :class:`AnalyticsReader` because that is the key the
    composition root publishes under and the key the state looks up — a stub
    behind the same key proves the lookup, which a hand-injected collaborator
    would not.
    """

    def __init__(self) -> None:
        self.totals_error: Exception | None = None
        self.series_error: Exception | None = None
        self.asked: list[int] = []

    def totals(self) -> ArchiveTotals:
        if self.totals_error is not None:
            raise self.totals_error
        return ArchiveTotals(
            messages=12_400,
            unidentified=124,
            groups=3,
            topics=4,
            templates=2,
            co_addressed=9,
        )

    def archived_per_day(self, *, days: int) -> tuple[ArchivedDay, ...]:
        self.asked.append(days)
        if self.series_error is not None:
            raise self.series_error
        last = NOW.date()
        return tuple(
            ArchivedDay(
                day=(last - timedelta(days=offset)).isoformat(),
                messages=0 if offset else 7,
                bytes=0 if offset else 3 * GIGABYTE,
            )
            for offset in reversed(range(days))
        )


class StubSearch:
    """Only the three members the dashboard asks a search for."""

    def __init__(self) -> None:
        self.available = True
        self.error: Exception | None = None

    def coverage(self) -> VectorCoverage:
        if self.error is not None:
            raise self.error
        return VectorCoverage(
            model="text-embedding-3-small",
            total=12_400,
            embedded=6_200,
            unembeddable=0,
        )

    def index_dimension(self) -> int:
        if self.error is not None:
            raise self.error
        return 1536


class StubStorage:
    """Three measured paths, with absolute paths on every one of them."""

    def __init__(self) -> None:
        self.error: Exception | None = None

    def usage(self) -> StorageUsage:
        if self.error is not None:
            raise self.error
        return StorageUsage(
            paths=(
                PathUsage(
                    label="Mailstore",
                    path=Path("/srv/mail-archive/.state/mailstore"),
                    used_bytes=3 * GIGABYTE,
                    file_count=1200,
                    total_bytes=8 * GIGABYTE,
                    free_bytes=5 * GIGABYTE,
                ),
                PathUsage(
                    label="Graph",
                    path=Path("/srv/mail-archive/.state/falkordb"),
                    used_bytes=GIGABYTE,
                    file_count=4,
                    total_bytes=8 * GIGABYTE,
                    free_bytes=5 * GIGABYTE,
                ),
                PathUsage(
                    label="Database",
                    path=Path("/srv/mail-archive/.state/mail-archive.db"),
                    used_bytes=GIGABYTE // 2,
                    file_count=1,
                    total_bytes=8 * GIGABYTE,
                    free_bytes=5 * GIGABYTE,
                ),
            )
        )


class StubHealth:
    """The graph server, reachable and new enough for a KNN."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.reachable = True

    async def status(self) -> GraphServerStatus:
        if self.error is not None:
            raise self.error
        return GraphServerStatus(
            mode=GraphServerMode.LOCAL,
            endpoint="localhost:6379",
            reachable=self.reachable,
            checked_at=NOW,
            redis_version="7.2.4",
            falkordb_version="4.0.9",
        )


class Sessions:
    """A session factory over one temporary SQLite file."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    @contextlib.asynccontextmanager
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        async with self._factory() as session:
            yield session
            await session.commit()


@pytest.fixture
def reader() -> StubReader:
    return StubReader()


@pytest.fixture
def search() -> StubSearch:
    return StubSearch()


@pytest.fixture
def storage() -> StubStorage:
    return StubStorage()


@pytest.fixture
def health() -> StubHealth:
    return StubHealth()


@pytest.fixture
def published(
    reader: StubReader,
    search: StubSearch,
    storage: StubStorage,
    health: StubHealth,
) -> Iterator[None]:
    """Every service the dashboard reads, left where the composition root
    leaves it."""
    services = service_registry()
    saved = services.snapshot()
    services.register_as(AnalyticsReader, cast(AnalyticsReader, reader))
    services.register_as(SemanticSearch, cast(SemanticSearch, search))
    services.register_as(StorageReader, cast(StorageReader, storage))
    services.register_as(GraphHealth, cast(GraphHealth, health))
    services.register_as(
        SemanticControl,
        SemanticControl(
            current=lambda: SemanticConfig(dimension=1536),
            reload=_never_reloads,
            reindex=_never_reindexes,
        ),
    )
    yield
    services.restore(saved)


async def _never_reloads() -> SemanticConfig:  # pragma: no cover - never called
    return SemanticConfig()


async def _never_reindexes() -> int:  # pragma: no cover - never called
    return 0


@pytest.fixture
async def database(tmp_path) -> AsyncIterator[Sessions]:
    """An archive with one broken account, one failed job and one lost message."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dashboard.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = Sessions(async_sessionmaker(engine, expire_on_commit=False))
    async with sessions() as session:
        broken = MailAccountEntity(
            provider="imap",
            display_name="Work",
            email_address="jens@example.com",
            status=AccountStatus.AUTH_ERROR,
            last_error="the password was refused",
            last_sync_at=NOW,
        )
        healthy = MailAccountEntity(
            provider="gmail",
            display_name="Private",
            email_address="private@example.com",
        )
        session.add_all([broken, healthy])
        await session.flush()
        session.add_all(
            [
                MailSyncJobEntity(kind=SyncJobKind.IMPORT, state=SyncJobState.QUEUED),
                MailSyncJobEntity(kind=SyncJobKind.IMPORT, state=SyncJobState.QUEUED),
                MailSyncJobEntity(
                    kind=SyncJobKind.DERIVE,
                    state=SyncJobState.FAILED,
                    error="the graph went away",
                    finished_at=NOW,
                ),
                MailFailedMessageEntity(
                    account_id=broken.id,
                    provider_message_id="18f2",
                    reason="unparseable",
                    detail="Date: header from the year 9999",
                    occurred_at=NOW,
                ),
                UserEntity(email="jens@example.com", name="Jens"),
                UserEntity(email="ada@example.com", name="Ada"),
            ]
        )
    with patch(f"{READS}.get_asyncdb_session", sessions):
        yield sessions
    await engine.dispose()


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> DashboardState:
    """The dashboard as an administrator sees it. Every other case says so."""
    instance = DashboardState()
    signed_in_as(instance, FakeUser(is_admin=True), monkeypatch)
    return instance


async def _load(state: DashboardState) -> None:
    """Invoke the page's ``on_load`` the way Reflex invokes a background task."""
    await DashboardState.load.fn(state)  # ty: ignore[unresolved-attribute]


async def _choose(state: DashboardState, value: str) -> None:
    """The range switch, same reason as :func:`_load`."""
    await DashboardState.choose_range.fn(state, value)  # ty: ignore[unresolved-attribute]


def _service(rows: list[ServiceView], name: str) -> bool:
    """Whether the named row of a checklist came back up."""
    for one in rows:
        if one.name == name:
            return one.up
    raise AssertionError(f"no service is called {name!r}")


def _text_of(state: DashboardState) -> str:
    """Everything the state would send to a browser, as one searchable string."""
    return " ".join(
        [
            state.archived,
            state.accounts,
            state.queued,
            state.users,
            state.last_archived,
            *(f"{one.label} {one.caption} {one.detail}" for one in state.health),
            *(f"{one.label} {one.caption} {one.detail}" for one in state.storage),
            *(f"{one.message} {one.when}" for one in state.notifications),
            *(one.name for one in state.services),
            state.archive_error,
            state.counts_error,
            state.series_error,
            state.storage_error,
            state.notifications_error,
        ]
    )


@pytest.mark.usefixtures("published", "database")
class TestOneRefresh:
    """What ``load`` leaves behind when everything answers."""

    async def test_every_panel_fills(self, state: DashboardState) -> None:
        await _load(state)

        assert state.archived == "12,400"
        assert state.accounts == "2"
        assert state.queued == "2"
        assert state.users == "2"
        assert state.last_archived != UNKNOWN
        assert state.health, "the archive-health meters are empty"
        assert state.storage, "the disk meters are empty"
        assert state.services, "the services checklist is empty"
        assert state.messages_series, "the messages chart has no points"
        assert state.storage_series, "the storage chart has no points"
        assert state.notifications, "an archive with three faults reports none"

    async def test_no_panel_is_left_spinning(self, state: DashboardState) -> None:
        await _load(state)

        assert not state.loading_archive
        assert not state.loading_counts
        assert not state.loading_series
        assert not state.loading_storage
        assert not state.loading_notifications
        assert not state.loading_services

    async def test_nothing_reports_an_error(self, state: DashboardState) -> None:
        await _load(state)

        assert state.archive_error == ""
        assert state.counts_error == ""
        assert state.series_error == ""
        assert state.storage_error == ""
        assert state.notifications_error == ""

    async def test_the_services_checklist_carries_no_endpoint(
        self, state: DashboardState
    ) -> None:
        """Booleans only. A host, a port or a version is administration."""
        await _load(state)

        assert [one.name for one in state.services]
        assert all(isinstance(one.up, bool) for one in state.services)
        for forbidden in ("localhost", "6379", "7.2.4", "4.0.9"):
            assert forbidden not in _text_of(state)

    async def test_the_last_archived_tile_comes_from_the_series(
        self, state: DashboardState, reader: StubReader
    ) -> None:
        """§1.3: one statement feeds both charts and this tile.

        A graph that is down takes all three with it, and nothing reaches for a
        second source in SQLite to fill the gap.
        """
        reader.series_error = ConnectionError("graph is down")

        await _load(state)

        assert state.last_archived == UNKNOWN
        assert state.messages_series == []
        assert state.storage_series == []
        assert state.archived == "12,400"


@pytest.mark.usefixtures("published", "database")
class TestOneDeadPanel:
    """A source that throws must cost its own panel and nothing else."""

    async def test_a_dead_graph_leaves_the_database_counts_standing(
        self, state: DashboardState, reader: StubReader
    ) -> None:
        reader.totals_error = ConnectionError("graph is down")

        await _load(state)

        assert state.archive_error
        assert state.archived == UNKNOWN
        assert state.health == []
        assert state.accounts == "2"
        assert state.users == "2"
        assert state.storage, "the disk panel went down with the graph"

    async def test_an_unreadable_disk_costs_only_the_disk_panel(
        self, state: DashboardState, storage: StubStorage
    ) -> None:
        storage.error = OSError("the volume is gone")

        await _load(state)

        assert state.storage_error
        assert state.storage == []
        assert state.archived == "12,400"
        assert state.notifications

    async def test_a_projection_that_throws_is_caught_with_its_read(
        self, state: DashboardState, reader: StubReader, monkeypatch
    ) -> None:
        """The read is not the only half that can fail.

        A projection renders dates, and a ``Date:`` header is whatever a sender
        wrote — one archived mail from the year 9999 raised ``OverflowError``
        out of every listing at once. So the projection is inside the same
        guard as the read, and this is what proves it.
        """

        def explode(_days: object) -> list[dict[str, Any]]:
            raise OverflowError("date value out of range")

        monkeypatch.setattr("mailarc_ui.dashboard.state.messages_points", explode)

        await _load(state)

        assert state.series_error
        assert state.archived == "12,400"
        assert state.storage


@pytest.mark.usefixtures("published", "database")
class TestTheRangeSwitch:
    """One read, two series, three widths."""

    async def test_it_asks_the_graph_once_for_both_charts(
        self, state: DashboardState, reader: StubReader
    ) -> None:
        await _choose(state, "week")

        assert reader.asked == [days_in("week")]
        assert len(state.messages_series) == days_in("week")
        assert len(state.storage_series) == days_in("week")

    async def test_it_reshapes_both_series(
        self, state: DashboardState, reader: StubReader
    ) -> None:
        await _choose(state, "week")
        weekly = len(state.messages_series)

        await _choose(state, "year")

        assert state.range == "year"
        assert len(state.messages_series) == days_in("year") != weekly
        assert len(state.storage_series) == len(state.messages_series)

    async def test_an_unknown_range_falls_back_rather_than_raising(
        self, state: DashboardState, reader: StubReader
    ) -> None:
        """The value arrives over a socket; a browser is not the only caller."""
        await _choose(state, "decade")

        assert reader.asked == [days_in("month")]
        assert state.messages_series


@pytest.mark.usefixtures("published", "database")
class TestWhoIsAsking:
    """``/`` is public, so this is the gate that keeps private data off it."""

    async def test_an_administrator_sees_the_notifications_and_the_paths(
        self, state: DashboardState
    ) -> None:
        await _load(state)

        assert "jens@example.com" in _text_of(state)
        assert "the password was refused" in _text_of(state)
        assert "/srv/mail-archive/.state/mailstore" in _text_of(state)

    async def test_a_signed_in_visitor_who_is_not_an_administrator_sees_neither(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = DashboardState()
        signed_in_as(instance, FakeUser(is_admin=False), monkeypatch)

        await _load(instance)

        assert instance.notifications == []
        assert "jens@example.com" not in _text_of(instance)
        assert "the password was refused" not in _text_of(instance)
        assert "/srv/mail-archive" not in _text_of(instance)

    async def test_an_anonymous_visitor_fails_the_gate_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = DashboardState()
        nobody_can_be_established(instance, monkeypatch)

        await _load(instance)

        assert instance.notifications == []
        assert "jens@example.com" not in _text_of(instance)
        assert "/srv/mail-archive" not in _text_of(instance)

    async def test_the_public_panels_still_answer_a_visitor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of a public dashboard: a refusal is not an empty page."""
        instance = DashboardState()
        nobody_can_be_established(instance, monkeypatch)

        await _load(instance)

        assert instance.archived == "12,400"
        assert instance.accounts == "2"
        assert instance.health
        assert instance.services
        assert instance.messages_series
        assert instance.storage, "a visitor loses the paths, not the panel"

    async def test_a_refused_notifications_panel_reads_as_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not an error, and not a count that says how many faults exist."""
        instance = DashboardState()
        signed_in_as(instance, FakeUser(is_admin=False), monkeypatch)

        await _load(instance)

        assert instance.notifications_error == ""
        assert instance.loading_notifications is False
        assert instance.notifications == []

    async def test_a_visitor_keeps_the_disk_ratios(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Labels, ratios and percentages are public; the paths are not."""
        instance = DashboardState()
        signed_in_as(instance, FakeUser(is_admin=False), monkeypatch)

        await _load(instance)

        assert [one.label for one in instance.storage] == [
            "Mailstore",
            "Graph",
            "Database",
        ]
        assert all(one.percent > 0 for one in instance.storage)
        assert all(one.detail == "" for one in instance.storage)


@pytest.mark.usefixtures("published", "database")
class TestWhatAFailureIsAllowedToSay:
    """A panel's error string is rendered on ``/``, which anybody may open.

    So it is written here and never quoted from an exception. The exceptions
    these reads actually raise carry the graph's host and port
    (``ConnectionError: Error 61 connecting to 127.0.0.1:6379``) and absolute
    filesystem paths (``PermissionError: … '/srv/mail-archive/.state/…'``) —
    the two facts the services card's own docstring says this page must not
    print. ``logger.exception`` still has all of it.
    """

    async def test_a_failing_graph_read_never_quotes_the_endpoint(
        self, reader: StubReader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = DashboardState()
        nobody_can_be_established(instance, monkeypatch)
        reader.totals_error = ConnectionError(
            "Error 61 connecting to 127.0.0.1:6379. Connection refused."
        )

        await _load(instance)

        assert instance.archive_error
        printed = _text_of(instance)
        for forbidden in ("6379", "127.0.0.1", "Error 61", "ConnectionError"):
            assert forbidden not in printed

    async def test_a_failing_disk_read_never_quotes_the_path(
        self, storage: StubStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = DashboardState()
        nobody_can_be_established(instance, monkeypatch)
        storage.error = PermissionError(
            13, "Permission denied", "/srv/mail-archive/.state/mailstore/ab/cd"
        )

        await _load(instance)

        assert instance.storage_error
        assert "/srv/mail-archive" not in _text_of(instance)

    async def test_a_failing_series_read_never_quotes_the_endpoint(
        self, reader: StubReader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instance = DashboardState()
        nobody_can_be_established(instance, monkeypatch)
        reader.series_error = ConnectionError(
            "Error 61 connecting to 127.0.0.1:6379. Connection refused."
        )

        await _load(instance)

        assert instance.series_error
        assert "6379" not in _text_of(instance)

    async def test_each_panel_still_says_which_read_failed(
        self, state: DashboardState, storage: StubStorage
    ) -> None:
        """Sanitised is not silent. A reader has to be able to tell a quiet
        archive from a broken one, which is what the error string is for."""
        storage.error = OSError("the volume is gone")

        await _load(state)

        assert "disk" in state.storage_error.lower()
        assert state.archive_error == ""


@pytest.mark.usefixtures("published", "database")
class TestTheServicesChecklistWhenSomethingIsDown:
    """The card §1.3 promises will say *why* the graph tiles show an em dash.

    ``services_of`` was written for exactly this: every argument is optional and
    ``None`` means "could not ask", which reads as down. A read that failed has
    to reach it as a ``None`` rather than blank the whole checklist out behind
    an alert — the case the card exists for is the case it must still render.
    """

    async def test_a_failing_vector_read_still_renders_the_checklist(
        self, search: StubSearch, state: DashboardState
    ) -> None:
        search.error = ConnectionError(
            "Error 61 connecting to 127.0.0.1:6379. Connection refused."
        )

        await _load(state)

        assert [one.name for one in state.services]
        assert _service(state.services, "Embedder configured") is False
        assert _service(state.services, "Vector index dimension matches") is False

    async def test_an_unreachable_graph_still_lists_every_service(
        self, search: StubSearch, health: StubHealth, state: DashboardState
    ) -> None:
        """The whole checklist, with the rows that could not be asked grey."""
        search.error = ConnectionError("graph is down")
        health.reachable = False

        await _load(state)

        assert len(state.services) == 5
        assert _service(state.services, "Graph server reachable") is False
        assert _service(state.services, "Embedder configured") is False

    async def test_the_checklist_is_split_before_its_last_two_rows(
        self, state: DashboardState
    ) -> None:
        """§5c: one dotted divider, before the final group. The state says
        where the split falls so no component counts rows."""
        await _load(state)

        assert state.services_split == len(state.services) - 2

    def test_an_empty_checklist_has_no_split_to_draw(self) -> None:
        assert DashboardState().services_split == 0


class TestHowItPrints:
    """Pure projections: a value in, the string the reference shows out."""

    @pytest.mark.parametrize(
        ("value", "printed"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (3 * 1024**3, "3.0 GB"),
            (7_500 * 1024**2, "7.3 GB"),
            (2 * 1024**4, "2.0 TB"),
        ],
    )
    def test_bytes_read_as_a_size(self, value: int, printed: str) -> None:
        assert human_bytes(value) == printed

    def test_a_timestamp_reads_as_the_reference_prints_it(self) -> None:
        """``Aug 7, 2025. 23:01`` — in the reader's own zone.

        Built as a local wall clock and given the local offset, so the
        assertion is about the *shape* of the string and holds in every zone
        this runs in. What the conversion itself does is the next test.
        """
        moment = datetime(2025, 8, 7, 23, 1).astimezone()  # noqa: DTZ001

        printed = moment_label(moment)

        assert printed == "Aug 7, 2025. 23:01"

    def test_a_naive_timestamp_is_read_as_the_archive_wrote_it(self) -> None:
        """SQLite hands back naive datetimes; every stamp in this archive is UTC.

        Read as local time instead, a stamp would silently move by hours.
        """
        assert moment_label(datetime(2025, 8, 7, 23, 1)) == moment_label(  # noqa: DTZ001
            datetime(2025, 8, 7, 23, 1, tzinfo=UTC)
        )

    def test_a_day_reads_without_a_time(self) -> None:
        assert day_label("2025-08-07") == "Aug 7, 2025"

    @pytest.mark.parametrize(
        "value", ["", "not-a-day", "2025-13-01", "9999-99-99", "None"]
    )
    def test_an_unreadable_day_is_an_em_dash(self, value: str) -> None:
        assert day_label(value) == UNKNOWN

    def test_a_missing_timestamp_is_an_em_dash(self) -> None:
        assert moment_label(None) == UNKNOWN

    def test_counts_carry_thousands_separators(self) -> None:
        assert thousands(12_400) == "12,400"
        assert thousands(0) == "0"

    def test_a_percentage_is_whole(self) -> None:
        assert percent_label(0.0) == "0%"
        assert percent_label(72.4) == "72%"
        assert percent_label(100.0) == "100%"

    def test_the_range_table_is_closed(self) -> None:
        assert days_in("week") == 7
        assert days_in("month") == 30
        assert days_in("year") == 365
        assert days_in("fortnight") == days_in("month")


class TestWhatEachPanelSaysWhenItKnowsNothing:
    """The empty cases, which is what most of a fresh installation renders."""

    def test_a_ratio_with_nothing_to_divide_by_is_nought(self) -> None:
        """An archive that holds no messages has no coverage — not a crash."""
        assert ratio_percent(5, 0) == 0.0

    def test_a_ratio_cannot_overflow_its_track(self) -> None:
        """Two counts read from two statements can disagree at a commit."""
        assert ratio_percent(11, 10) == 100.0

    def test_a_series_that_archived_nothing_has_no_last_day(self) -> None:
        """A day with a nought on it is not "something was archived then"."""
        quiet = tuple(ArchivedDay(day=f"2025-08-0{one}") for one in range(1, 4))

        assert last_archived_label(quiet) == UNKNOWN
        assert last_archived_label(()) == UNKNOWN

    def test_a_service_nobody_could_ask_about_reads_as_down(self) -> None:
        """Grey and not green: a row that stayed up because the question
        failed would be worse than one that admits it does not know."""
        rows = services_of(None, None, None)

        assert [one.up for one in rows] == [False] * len(rows)
        assert all(one.name for one in rows)

    def test_a_worker_is_reported_missing_only_when_work_is_waiting(self) -> None:
        """An idle worker and a missing one are indistinguishable from here.

        What is observable is the symptom: jobs queued and nothing claiming
        them. So an empty queue reads as healthy and a served one does too.
        """
        stalled = services_of(None, None, DashboardCounts(queued=3))
        idle = services_of(None, None, DashboardCounts())
        working = services_of(None, None, DashboardCounts(queued=3, running=1))

        assert _service(stalled, "Sync worker running") is False
        assert _service(idle, "Sync worker running") is True
        assert _service(working, "Sync worker running") is True

    def test_a_fault_with_no_timestamp_is_still_reported(self) -> None:
        """An unrecorded time is not a reason to stop naming a broken mailbox."""
        account = MailAccountEntity(
            provider="imap",
            display_name="Work",
            email_address="jens@example.com",
            status=AccountStatus.ERROR,
            last_error=None,
        )

        listed = notifications_of([], [account], [])

        assert len(listed) == 1
        assert "jens@example.com" in listed[0].message
        assert listed[0].when == UNKNOWN

    def test_no_panel_is_left_spinning_over_an_empty_answer(self) -> None:
        """Every flag starts true and every flag is cleared by one ``_apply``.

        A read that came back with nothing is still a read that came back, and
        a card that went on showing a placeholder over it would be claiming
        the archive had not answered.
        """
        state = DashboardState()

        assert state.loading_archive
        assert state.loading_counts
        assert state.loading_series
        assert state.loading_storage
        assert state.loading_notifications
        assert state.loading_services

        state._apply(Readout())

        assert not state.loading_archive
        assert not state.loading_counts
        assert not state.loading_series
        assert not state.loading_storage
        assert not state.loading_notifications
        assert not state.loading_services

    def test_an_empty_notifications_panel_is_not_a_list(self) -> None:
        """The one var the card branches on, and it says nothing about why."""
        state = DashboardState()

        assert state.has_notifications is False

        state.notifications = [NotificationView(message="something", when="now")]

        assert state.has_notifications is True


class TestWithoutAComposition:
    """Nothing is looked up at import, so an unpublished service is a sentence."""

    async def test_a_missing_reader_is_one_panel_saying_so(
        self, state: DashboardState
    ) -> None:
        services = service_registry()
        saved = services.snapshot()
        try:
            await _load(state)
        finally:
            services.restore(saved)

        assert state.archive_error
        assert state.archived == UNKNOWN
