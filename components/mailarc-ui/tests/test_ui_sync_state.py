"""Tests for :mod:`mailarc_ui.sync.state` — the rail's Sync button.

Against a real SQLite file, the real :class:`JobQueue` and the real
:class:`MailboxSweep`, for the reason ``test_ui_imports_state`` gives: the
state's whole job is to press something and then report what it finds, and a
double for either half would be free to agree with the report and be wrong.
What the sweep *decides* is pinned next door in ``test_jobs_sweep``; what is
pinned here is the pressing, the following and the sentence.

The wording is tested apart from all of it. :func:`announce` and
:func:`summarise` are pure functions over a :class:`SweepReport` and over the
finished jobs precisely so the ways each can turn out — everything already
running, everything waiting for a human, nothing there at all; a run that
brought mail in, a run that dropped some — can be told apart without a
database, because a rail 76px wide answers all of them with the same picture.

The toasts are checked through the events the handlers return, which is the
only place they exist: a toast is an event and not a component, so there is
nothing on a page to look at. Both are asserted on the script the event runs,
because that is what carries the sentence *and* the sonner level — a finished
run that arrived green when it dropped mail would pass every assertion made on
the text alone.

Only the clock and Reflex's own state lock are faked, so no test waits.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import reflex as rx
from appkit_commons.database.entities import Base
from reflex.event import EventSpec
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mailarc_core.database.entities import (
    AccountStatus,
    MailAccountEntity,
    MailSyncJobEntity,
    SyncJobState,
)
from mailarc_core.database.repositories import SyncCheckpointRepository
from mailarc_core.database.sqlite import install_pragmas
from mailarc_core.mail.model import MailProvider, ProviderDescriptor
from mailarc_core.mail.ports import MailSourcePort
from mailarc_sync.engine.engine import INCREMENTAL_SCOPE
from mailarc_sync.engine.registry import ProviderRegistry
from mailarc_sync.jobs import (
    JobKind,
    JobProgress,
    JobQueue,
    JobState,
    MailboxSweep,
    SessionFactory,
    SweepReport,
    SyncJob,
)
from mailarc_ui.sync import rail_sync_button
from mailarc_ui.sync.state import (
    NOTHING_TO_SYNC,
    SYNC_LABEL,
    SyncAllState,
    announce,
    mailboxes,
    messages,
    summarise,
)

STATE_MODULE = "mailarc_ui.sync.state"


@pytest.fixture
async def engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    """A fresh database file with the mail tables on it."""
    install_pragmas()
    created = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail-archive.db'}")
    async with created.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> SessionFactory:
    """One session per call, committed on the way out — appkit's semantics."""
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def open_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return open_session


@pytest.fixture
def queue(session_factory: SessionFactory) -> JobQueue:
    return JobQueue(session_factory)


@pytest.fixture
def registry() -> ProviderRegistry:
    """The fake provider, registered. Its factory is never called: a sweep that
    opened a mailbox would be the bug the descriptor exists to prevent."""
    built = ProviderRegistry()
    built.register(
        ProviderDescriptor(
            provider=MailProvider.FAKE,
            label="Folder of .eml files",
            supports_incremental=True,
        ),
        _refuse_to_open,
    )
    return built


def _refuse_to_open(account: MailAccountEntity, secret: str) -> MailSourcePort:
    raise AssertionError("the button opened a mailbox")


async def stored_account(
    session_factory: SessionFactory,
    *,
    address: str = "jens@example.com",
    status: str = AccountStatus.IDLE,
    armed: bool = True,
) -> int:
    """An enabled mailbox, by default one whose first import has finished."""
    async with session_factory() as session:
        account = MailAccountEntity(
            provider=MailProvider.FAKE,
            display_name="Work",
            email_address=address,
            enabled=True,
            status=status,
        )
        session.add(account)
        await session.flush()
        account_id = account.id
    if armed:
        async with session_factory() as session:
            await SyncCheckpointRepository().upsert_cursor(
                session, account_id, INCREMENTAL_SCOPE, "1000", 0
            )
    return account_id


@pytest.fixture
async def account_id(session_factory: SessionFactory) -> int:
    return await stored_account(session_factory)


@pytest.fixture
def state(
    queue: JobQueue,
    registry: ProviderRegistry,
    session_factory: SessionFactory,
) -> Iterator[SyncAllState]:
    """The state under test, with everything it builds pointed at our file.

    Three patches and not one: the state builds a queue, a registry and a
    sweep per call — which is the production path — and the sweep reads two
    tables of its own through a session factory that defaults to the
    application's. Only the wiring is replaced; the queue, the sweep and the
    database underneath them are the real ones.
    """

    def sweep(job_queue: JobQueue, providers: ProviderRegistry) -> MailboxSweep:
        return MailboxSweep(job_queue, providers, session_factory=session_factory)

    with (
        patch(f"{STATE_MODULE}.JobQueue", Mock(return_value=queue)),
        patch(f"{STATE_MODULE}.provider_registry", Mock(return_value=registry)),
        patch(f"{STATE_MODULE}.MailboxSweep", Mock(side_effect=sweep)),
    ):
        instance = SyncAllState()
        instance.poll_interval = 0
        yield instance


async def end_job(
    session_factory: SessionFactory,
    job_id: int,
    *,
    done: int = 0,
    failed: int = 0,
) -> None:
    """Take a job out of the open states without running it.

    The counters are what a worker would have left behind, and they are the
    only reason the finished toast has a number in it.
    """
    async with session_factory() as session:
        job = await session.get(MailSyncJobEntity, job_id)
        assert job is not None
        job.state = SyncJobState.SUCCEEDED
        job.messages_done = done
        job.messages_failed = failed


async def _run_poll(state: SyncAllState) -> list[Any]:
    """Drive the background handler and collect whatever it yielded.

    Reflex refuses a direct ``state.poll()`` call on a background handler, so
    go through the EventHandler's wrapped function — which is now an async
    generator, because the end of a run is news and the loop is the only place
    that knows it happened.
    """
    return [event async for event in SyncAllState.poll.fn(state)]


def script_of(event: Any) -> str:
    """The JavaScript one toast event runs.

    Both halves of a toast live in there — ``["success"]`` or ``["warning"]``
    for the sonner level, and the sentence — and neither is reachable off the
    :class:`EventSpec` as data. A test that read only the text would pass on a
    dropped-mail run announced in green.
    """
    return " ".join(str(value) for _, value in event.args)


def toasts(events: list[Any]) -> list[str]:
    """The scripts of the toasts among the returned events, in order."""
    return [script_of(event) for event in events if isinstance(event, EventSpec)]


def polls(events: list[Any]) -> list[Any]:
    """The follow-the-archive events among them — everything not a toast."""
    return [event for event in events if not isinstance(event, EventSpec)]


def _ended(*, done: int = 0, failed: int = 0) -> SyncJob:
    """A finished job carrying nothing but the two numbers being counted."""
    return SyncJob(
        id=1,
        kind=JobKind.INCREMENTAL,
        state=JobState.SUCCEEDED,
        progress=JobProgress(total=done + failed, done=done, failed=failed),
    )


def _stopping_sleep(state: SyncAllState, after: int = 1) -> AsyncMock:
    """A sleep that ends the loop, so no test waits on a real clock."""
    calls = {"n": 0}

    async def sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= after:
            state.polling = False

    return AsyncMock(side_effect=sleep)


def _forbidden_sleep() -> AsyncMock:
    """A sleep that fails the test instead of hanging it."""

    async def sleep(_seconds: float) -> None:
        raise AssertionError("the poll slept when it should have stopped")

    return AsyncMock(side_effect=sleep)


class _MultiPatch:
    def __init__(self, *patchers: Any) -> None:
        self._patchers = patchers

    def __enter__(self) -> _MultiPatch:
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        for patcher in reversed(self._patchers):
            patcher.stop()
        return False


def _patched_loop(sleep: AsyncMock) -> _MultiPatch:
    """Fake the clock and the state lock the background task takes."""
    return _MultiPatch(
        patch.object(asyncio, "sleep", sleep),
        patch.object(SyncAllState, "__aenter__", AsyncMock()),
        patch.object(SyncAllState, "__aexit__", AsyncMock(return_value=False)),
    )


class TestTheSentence:
    """The three ways a press can queue nothing, told apart.

    Pure over a report, so each one is a line rather than a fixture: the rail
    draws all three identically and a person can only tell them apart by
    reading the tooltip.
    """

    def test_an_archive_with_no_mailboxes_says_so(self) -> None:
        assert announce(SweepReport()) == NOTHING_TO_SYNC

    def test_a_plain_sweep_counts_what_it_started(self) -> None:
        assert announce(SweepReport(queued=(1, 2, 3))) == "Syncing 3 mailboxes."

    def test_one_mailbox_is_singular(self) -> None:
        assert announce(SweepReport(queued=(1,))) == "Syncing 1 mailbox."
        assert mailboxes(1) == "1 mailbox"
        assert mailboxes(2) == "2 mailboxes"

    def test_a_mailbox_already_running_is_named_beside_the_new_ones(self) -> None:
        sentence = announce(SweepReport(queued=(1,), busy=(7,)))

        assert sentence == "Syncing 1 mailbox; 1 mailbox already syncing."

    def test_a_press_that_started_nothing_still_says_what_is_going_on(self) -> None:
        """The outcome a silent button would render as a broken one."""
        assert announce(SweepReport(busy=(7, 8))) == "2 mailboxes already syncing."

    def test_a_blocked_mailbox_asks_for_a_person(self) -> None:
        assert announce(SweepReport(blocked=(7,))) == "1 mailbox needs attention."
        assert announce(SweepReport(blocked=(7, 8))) == "2 mailboxes need attention."

    def test_a_finished_run_is_counted_in_mail_and_not_in_mailboxes(self) -> None:
        """Which is what the press was for. Nobody syncs to collect jobs."""
        jobs = [_ended(done=40), _ended(done=17)]

        assert summarise(jobs) == ("Finished: synced 57 messages.", "success")

    def test_one_message_is_singular(self) -> None:
        assert summarise([_ended(done=1)]) == ("Finished: synced 1 message.", "success")
        assert messages(1) == "1 message"
        assert messages(2) == "2 messages"

    def test_a_run_that_arrived_empty_still_says_so(self) -> None:
        """A delta over a quiet mailbox. Zero is an answer; silence is not."""
        assert summarise([_ended()]) == ("Finished: synced 0 messages.", "success")

    def test_a_run_that_dropped_mail_is_not_green(self) -> None:
        """``done`` and ``failed`` are disjoint, so the failures are not in the
        count and would otherwise be invisible — the worst way to lose mail."""
        jobs = [_ended(done=40, failed=2)]

        assert summarise(jobs) == ("Finished: synced 40 messages, 2 failed.", "warning")

    def test_every_bucket_at_once_reads_as_one_sentence(self) -> None:
        sentence = announce(SweepReport(queued=(1,), busy=(2,), blocked=(3,)))

        assert sentence == (
            "Syncing 1 mailbox; 1 mailbox already syncing; 1 mailbox needs attention."
        )


class TestDefaults:
    def test_the_rail_is_still_before_anything_is_pressed(self, state) -> None:
        assert state.syncing is False
        assert state.active == 0
        assert state.polling is False
        assert state.message == ""

    def test_the_tooltip_is_the_button_name_until_there_is_news(self, state) -> None:
        assert state.hint == SYNC_LABEL

    def test_the_tooltip_becomes_the_progress_while_it_runs(self, state) -> None:
        state.syncing = True
        state.active = 3

        assert state.hint == "Syncing 3 mailboxes…"

    def test_the_tooltip_keeps_the_last_news_once_it_stops(self, state) -> None:
        """Which is what a press that queued nothing has instead of a spinner."""
        state.message = NOTHING_TO_SYNC

        assert state.hint == NOTHING_TO_SYNC


class TestPressingIt:
    async def test_it_queues_a_job_for_every_mailbox(
        self, state, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        await stored_account(session_factory, address="second@example.com")

        await state.sync_all()

        open_jobs = await queue.find_all_open((JobKind.INCREMENTAL, JobKind.IMPORT))
        assert len(open_jobs) == 2
        assert state.message == "Syncing 2 mailboxes."

    async def test_it_starts_the_animation_and_the_poll(
        self, state, account_id: int
    ) -> None:
        events = await state.sync_all()

        assert state.syncing is True
        assert state.active == 1
        assert state.polling is True
        assert polls(events) != []

    async def test_a_mailbox_nobody_has_imported_gets_a_full_import(
        self, state, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """The button's promise on a fresh install, where the schedule would
        have queued nothing at all."""
        await stored_account(session_factory, armed=False)

        await state.sync_all()

        open_jobs = await queue.find_all_open((JobKind.INCREMENTAL, JobKind.IMPORT))
        assert [job.kind for job in open_jobs] == [JobKind.IMPORT]

    async def test_pressing_it_twice_does_not_queue_a_second_run(
        self, state, queue: JobQueue, account_id: int
    ) -> None:
        """Two runs over one mailbox is an ``IntegrityError``, not a wasted job."""
        await state.sync_all()

        await state.sync_all()

        open_jobs = await queue.find_all_open((JobKind.INCREMENTAL, JobKind.IMPORT))
        assert len(open_jobs) == 1
        assert state.message == "1 mailbox already syncing."
        assert state.syncing is True

    async def test_an_archive_with_no_mailboxes_says_so_and_does_not_spin(
        self, state
    ) -> None:
        events = await state.sync_all()

        assert state.message == NOTHING_TO_SYNC
        assert state.syncing is False
        assert state.polling is False
        assert polls(events) == []

    async def test_a_mailbox_waiting_for_a_re_consent_asks_for_a_person(
        self, state, session_factory: SessionFactory
    ) -> None:
        await stored_account(session_factory, status=AccountStatus.AUTH_ERROR)

        await state.sync_all()

        assert state.message == "1 mailbox needs attention."
        assert state.syncing is False

    async def test_a_press_that_cannot_reach_the_database_leaves_a_sentence(
        self, state, account_id: int, caplog
    ) -> None:
        """And not a button that looks pressed. §7.6: nothing is dropped
        without a trace."""
        with patch.object(
            type(state),
            "_sweep",
            Mock(side_effect=RuntimeError("database is locked")),
        ):
            events = await state.sync_all()

        assert state.starting is False
        assert state.syncing is False
        assert polls(events) == []
        assert "see the log" in state.message
        assert "database is locked" in caplog.text

    async def test_a_press_already_in_flight_is_not_a_second_sweep(
        self, state, account_id: int
    ) -> None:
        """The double click. ``starting`` is the guard, and it is cleared in a
        ``finally`` so a failed press does not disable the button forever."""
        state.starting = True

        assert await state.sync_all() == []
        assert state.message == ""


class TestArrivingOnAPage:
    """``on_mount``: the rail has to be honest about a sync it did not start."""

    async def test_it_picks_up_a_job_somebody_else_queued(
        self, state, queue: JobQueue, account_id: int
    ) -> None:
        """An import runs for hours; every navigation remounts this button."""
        await queue.enqueue(JobKind.IMPORT, account_id)

        follow = await state.adopt()

        assert state.syncing is True
        assert state.active == 1
        assert state.polling is True
        assert follow is not None

    async def test_a_quiet_archive_starts_no_poll(self, state, account_id) -> None:
        follow = await state.adopt()

        assert state.syncing is False
        assert state.polling is False
        assert follow is None

    async def test_a_second_mount_does_not_start_a_second_poll(
        self, state, queue: JobQueue, account_id: int
    ) -> None:
        await queue.enqueue(JobKind.IMPORT, account_id)
        await state.adopt()

        assert await state.adopt() is None
        assert state.polling is True

    async def test_a_read_that_fails_leaves_the_spinner_where_it_was(
        self, state, caplog
    ) -> None:
        """The archive did not stop syncing because this process could not ask."""
        state.syncing = True
        state.active = 2
        with patch.object(
            type(state),
            "_open_count",
            AsyncMock(side_effect=RuntimeError("database is locked")),
        ):
            await state.adopt()

        assert state.syncing is True
        assert state.active == 2
        assert "database is locked" in caplog.text


class TestFollowingIt:
    async def test_the_poll_stops_when_the_last_job_ends(
        self, state, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await state.adopt()
        await end_job(session_factory, job_id)

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.syncing is False
        assert state.polling is False

    async def test_the_poll_keeps_going_while_a_job_is_open(
        self, state, queue: JobQueue, account_id: int
    ) -> None:
        await queue.enqueue(JobKind.IMPORT, account_id)
        await state.adopt()

        sleep = _stopping_sleep(state)
        with _patched_loop(sleep):
            await _run_poll(state)

        assert sleep.await_count == 1
        assert state.syncing is True

    async def test_a_read_that_fails_does_not_end_the_loop(
        self, state, queue: JobQueue, account_id: int, caplog
    ) -> None:
        """A spinner that stopped on one locked database would say the sync had
        finished."""
        await queue.enqueue(JobKind.IMPORT, account_id)
        await state.adopt()

        sleep = _stopping_sleep(state)
        with (
            _patched_loop(sleep),
            patch.object(
                type(state),
                "_open_count",
                AsyncMock(side_effect=RuntimeError("database is locked")),
            ),
        ):
            await _run_poll(state)

        assert sleep.await_count == 1
        assert state.syncing is True
        assert "Could not read the open sync jobs" in caplog.text

    async def test_a_stop_that_lands_during_a_read_is_honoured(
        self, state, queue: JobQueue, account_id: int
    ) -> None:
        """The race a page navigating away opens: the read happens outside the
        state lock, so ``stop_polling`` can arrive between asking the queue and
        being allowed to write down the answer. The loop has to drop the answer
        rather than restart the spinner it was just told to stop."""
        await queue.enqueue(JobKind.IMPORT, account_id)
        await state.adopt()
        state.syncing = False

        async def stop_mid_read() -> int:
            state.polling = False
            return 1

        with (
            _patched_loop(_forbidden_sleep()),
            patch.object(type(state), "_open_count", AsyncMock(wraps=stop_mid_read)),
        ):
            await _run_poll(state)

        assert state.syncing is False

    async def test_stop_polling_ends_it(self, state) -> None:
        state.polling = True

        state.stop_polling()

        assert state.polling is False

    async def test_a_loop_asked_to_stop_returns_without_reading(self, state) -> None:
        state.polling = False

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.polling is False


class TestTheToasts:
    """The two moments a person who is not pointing at the rail hears about.

    Asserted on the script the event runs, level included: a finished run that
    dropped mail and arrived green would read as a success on every assertion
    made on the sentence alone.
    """

    async def test_a_press_says_what_it_started(
        self, state, session_factory: SessionFactory, account_id: int
    ) -> None:
        await stored_account(session_factory, address="second@example.com")

        events = await state.sync_all()

        (announced,) = toasts(events)
        assert "Syncing 2 mailboxes." in announced
        assert '["info"]' in announced

    async def test_a_press_that_started_nothing_arrives_amber(self, state) -> None:
        """The press with nothing to show for itself is the one that most needs
        a sentence — no spinner starts and nothing on the page moves."""
        events = await state.sync_all()

        (announced,) = toasts(events)
        assert NOTHING_TO_SYNC in announced
        assert '["warning"]' in announced

    async def test_a_press_that_could_not_reach_the_database_says_so(
        self, state, account_id: int
    ) -> None:
        with patch.object(
            type(state), "_sweep", Mock(side_effect=RuntimeError("database is locked"))
        ):
            events = await state.sync_all()

        (announced,) = toasts(events)
        assert "see the log" in announced
        assert '["error"]' in announced

    async def test_the_end_of_the_run_counts_the_mail_it_brought_in(
        self, state, session_factory: SessionFactory, account_id: int
    ) -> None:
        """The whole reason the press's job ids are kept: a spinner stopping
        says a run ended, and only this says what it was worth."""
        await stored_account(session_factory, address="second@example.com")
        await state.sync_all()
        first, second = state.watching
        await end_job(session_factory, first, done=40)
        await end_job(session_factory, second, done=17)

        with _patched_loop(_forbidden_sleep()):
            events = await _run_poll(state)

        (finished,) = toasts(events)
        assert "Finished: synced 57 messages." in finished
        assert '["success"]' in finished
        assert list(state.watching) == []

    async def test_the_tooltip_is_left_holding_what_the_toast_said(
        self, state, session_factory: SessionFactory, account_id: int
    ) -> None:
        """A toast is gone in six seconds. ``Syncing 1 mailbox.`` still under
        the cursor an hour after it finished would be the rail's only lie."""
        await state.sync_all()
        (job_id,) = state.watching
        await end_job(session_factory, job_id, done=3)

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.hint == "Finished: synced 3 messages."

    async def test_a_run_still_going_says_nothing_yet(
        self, state, account_id: int
    ) -> None:
        sleep = _stopping_sleep(state)
        await state.sync_all()

        with _patched_loop(sleep):
            events = await _run_poll(state)

        assert sleep.await_count == 1
        assert toasts(events) == []
        assert list(state.watching) != []

    async def test_a_sync_this_button_did_not_start_ends_quietly(
        self, state, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """``adopt`` follows the schedule's work and the accounts page's, and a
        count of somebody else's import is not an answer to a press nobody
        made."""
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await state.adopt()
        await end_job(session_factory, job_id, done=9)

        with _patched_loop(_forbidden_sleep()):
            events = await _run_poll(state)

        assert toasts(events) == []

    async def test_a_page_that_went_away_keeps_its_run_for_the_next_one(
        self, state, account_id: int
    ) -> None:
        """The loop ends without news, and the ids stay: the run is still going
        and the button is remounted on every navigation."""
        await state.sync_all()
        watched = list(state.watching)
        state.stop_polling()

        with _patched_loop(_forbidden_sleep()):
            events = await _run_poll(state)

        assert toasts(events) == []
        assert list(state.watching) == watched

    async def test_a_second_press_adds_to_what_the_first_is_watching(
        self, state, session_factory: SessionFactory, account_id: int
    ) -> None:
        """One count at the end, over both presses: a mailbox that was busy the
        first time round is part of the same sync as far as anybody watching is
        concerned."""
        await state.sync_all()
        await stored_account(session_factory, address="second@example.com")

        await state.sync_all()

        assert len(state.watching) == 2

    async def test_a_job_that_is_no_longer_there_is_left_out_of_the_count(
        self, state, session_factory: SessionFactory, account_id: int, caplog
    ) -> None:
        """And does not sink the toast. The run happened; a missing row is a
        gap in the arithmetic, not a reason to say nothing."""
        await state.sync_all()
        (job_id,) = state.watching
        await end_job(session_factory, job_id, done=5)
        state.watching = [*state.watching, 9999]

        with _patched_loop(_forbidden_sleep()):
            events = await _run_poll(state)

        (finished,) = toasts(events)
        assert "Finished: synced 5 messages." in finished
        assert "9999" in caplog.text

    async def test_a_read_that_fails_at_the_end_still_says_the_run_is_over(
        self,
        state,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
        caplog,
    ) -> None:
        """The spinner has already stopped by then. A toast withheld because
        the count could not be read would leave the press with no ending at
        all — §7.6 again: the failure is logged, not swallowed."""
        await state.sync_all()
        (job_id,) = state.watching
        await end_job(session_factory, job_id, done=5)

        with (
            _patched_loop(_forbidden_sleep()),
            patch.object(
                queue, "get", AsyncMock(side_effect=RuntimeError("database is locked"))
            ),
        ):
            events = await _run_poll(state)

        (finished,) = toasts(events)
        assert "Finished: synced 0 messages." in finished
        assert "database is locked" in caplog.text


class TestTheButton:
    def test_it_builds(self) -> None:
        """A prop appkit_mantine does not have only shows up when it is built."""
        assert isinstance(rail_sync_button(), rx.Component)

    def test_it_carries_the_flag_the_stylesheet_animates(self) -> None:
        """The animation is one attribute crossing the Var boundary; a button
        that stopped emitting it would look completely correct and never spin.
        """
        assert '"data-syncing":' in str(rail_sync_button().render())

    def test_it_names_itself_whatever_the_tooltip_currently_says(self) -> None:
        """The tooltip is the news and the label is the name. A control renamed
        by its own progress is a new control on every poll."""
        assert f'"aria-label":"{SYNC_LABEL}"' in str(rail_sync_button().render())
