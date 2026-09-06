"""Tests for the conversation repair in :mod:`mailarc_ui.repair`.

Against the real :class:`JobQueue` on a real SQLite file, for the reason the
rebuild control's tests give: the panel projects a job row and follows one, and
both are only worth anything if the counters it reads are the ones the queue
actually wrote. Only the clock and Reflex's own state lock are faked, so no
test waits.

The archive is a stand-in with one method, because the count is one call and
what it *means* is settled in ``mailarc-core`` against a real FalkorDB.

Two claims here are the panel's alone. **A finished repair re-reads the
count** — the number it was showing is the number the job just changed, and a
panel still saying "735 messages sit in no conversation" over a repaired
archive is the reason somebody presses the button twice. And **a repair reports
a result, not a position**: the row is written once at the end, so the sentence
appears when the job is over and never while it walks.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from appkit_commons.database.entities import Base
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mailarc_core.database.sqlite import install_pragmas
from mailarc_sync.jobs import JobKind, JobQueue, SessionFactory
from mailarc_ui.repair import ConversationRepairState
from mailarc_ui.repair.model import WHOLE, outside_label, repaired_label
from mailarc_ui.repair.state import GAVE_UP, READ_FAILED

STATE_MODULE = "mailarc_ui.repair.state"

WORKER = "worker-under-test"
LEASE_SECONDS = 60.0


class FakeArchive:
    """The archive's one answer, and how many times it was asked for it."""

    def __init__(self, outside: int = 3, error: Exception | None = None) -> None:
        self.outside = outside
        self.error = error
        self.asks = 0

    def count_unthreaded_messages(self) -> int:
        self.asks += 1
        if self.error is not None:
            raise self.error
        return self.outside


@pytest.fixture
async def engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    """A fresh database file with the mail tables on it."""
    install_pragmas()
    created = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail.db'}")
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
def archive() -> FakeArchive:
    return FakeArchive()


@pytest.fixture
def state(queue: JobQueue, archive: FakeArchive) -> Iterator[ConversationRepairState]:
    """The state under test, with the queue and the archive it builds patched.

    Both patches stay up for the whole test: the state looks each collaborator
    up inside every handler, which is the production path.
    """
    with (
        patch(f"{STATE_MODULE}.JobQueue", Mock(return_value=queue)),
        patch(f"{STATE_MODULE}.archive_reader", Mock(return_value=archive)),
    ):
        instance = ConversationRepairState()
        instance.poll_interval = 0
        yield instance


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
        patch.object(ConversationRepairState, "__aenter__", AsyncMock()),
        patch.object(
            ConversationRepairState, "__aexit__", AsyncMock(return_value=False)
        ),
    )


def _stopping_sleep(state: ConversationRepairState, after: int = 1) -> AsyncMock:
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


async def _load(state: ConversationRepairState) -> None:
    """The panel's ``on_load``, the way Reflex invokes a background task."""
    await ConversationRepairState.load.fn(state)


async def _run_poll(state: ConversationRepairState) -> None:
    await ConversationRepairState.poll.fn(state)


class TestTheCount:
    async def test_the_panel_says_how_much_sits_outside_a_conversation(
        self, state, archive
    ) -> None:
        archive.outside = 735

        await _load(state)

        assert state.outside == 735
        assert state.outside_message == "735 messages sit in no conversation."
        assert state.checked is True
        assert state.error == ""

    async def test_an_archive_that_is_whole_says_so_rather_than_nothing(
        self, state, archive
    ) -> None:
        """ "Nothing to repair" is a fact worth stating on a page somebody
        opened to check exactly that."""
        archive.outside = 0

        await _load(state)

        assert state.outside_message == WHOLE

    async def test_a_graph_that_does_not_answer_leaves_a_sentence(
        self, state, archive
    ) -> None:
        """Never the driver's own text: it carries a path out of this
        installation into a page that renders what it is given."""
        archive.error = RuntimeError("Error 61 connecting to 127.0.0.1:6379")

        await _load(state)

        assert state.error == READ_FAILED
        assert "6379" not in state.error
        assert state.checked is False
        assert state.can_repair is False

    def test_the_button_is_not_offered_before_the_archive_answers(self, state) -> None:
        assert state.can_repair is False


class TestRepairing:
    async def test_a_repair_is_one_job_about_the_whole_archive(
        self, state, queue
    ) -> None:
        await _load(state)

        result = await state.start_repair()

        job = await queue.get(state.job_id)
        assert job is not None
        assert job.kind == JobKind.REPAIR
        assert job.account_id is None
        assert state.job.active is True
        assert state.can_repair is False
        assert state.polling is True
        assert result is ConversationRepairState.poll

    async def test_a_running_repair_is_not_started_twice(self, state) -> None:
        """A second tab knows about no job, so the queue is asked before one
        more is added."""
        await _load(state)
        await state.start_repair()
        first = state.job_id

        assert await state.start_repair() is None
        assert state.job_id == first
        assert state.notice == "A repair is already running."

    async def test_a_finished_repair_no_longer_blocks_the_next_one(
        self, state, queue
    ) -> None:
        await _load(state)
        await state.start_repair()
        first = state.job_id
        await queue.succeed(first)

        assert await state.start_repair() is None  # already polling
        assert state.job_id != first
        assert state.job.active is True

    async def test_a_second_tab_follows_the_open_repair_instead_of_adding_one(
        self, queue, archive
    ) -> None:
        """``job_id`` is per page and starts at zero, so a reloaded tab knows
        about no repair. Two of them are harmless — the walk is idempotent —
        but the second would find nothing to do and report a repair that did
        nothing, which is worse to show than the truth."""
        with (
            patch(f"{STATE_MODULE}.JobQueue", Mock(return_value=queue)),
            patch(f"{STATE_MODULE}.archive_reader", Mock(return_value=archive)),
        ):
            first = ConversationRepairState()
            second = ConversationRepairState()
            first.poll_interval = second.poll_interval = 0
            await _load(first)
            await _load(second)
            await first.start_repair()

            await second.start_repair()

        assert second.job_id == first.job_id
        assert second.notice == "A repair is already running."

    async def test_a_queue_that_cannot_be_asked_still_queues_the_repair(
        self, state, queue
    ) -> None:
        """The adopt is an optimisation, not a gate: a queue that cannot answer
        would refuse the enqueue right after too, and that path has its own
        message."""
        await _load(state)

        with patch.object(
            queue, "find_open", AsyncMock(side_effect=RuntimeError("db hiccup"))
        ):
            await state.start_repair()

        assert state.job_id > 0
        assert state.notice == ""

    async def test_a_read_that_fails_leaves_the_last_job_standing(
        self, state, queue
    ) -> None:
        """A database hiccup is not news that the repair ended."""
        await _load(state)
        await state.start_repair()
        before = state.job

        with patch.object(
            queue, "get", AsyncMock(side_effect=RuntimeError("db hiccup"))
        ):
            await state._sync_job()  # noqa: SLF001 - the panel's own read

        assert state.job == before

    async def test_a_queue_that_refuses_leaves_a_message_not_a_traceback(
        self, state, queue
    ) -> None:
        await _load(state)

        with patch.object(
            queue, "enqueue", AsyncMock(side_effect=RuntimeError("db went away"))
        ):
            assert await state.start_repair() is None

        assert "db went away" in state.notice
        assert state.starting is False
        assert state.polling is False


class TestWhatAFinishedRunSays:
    async def test_the_result_is_the_tally_the_worker_wrote(self, state, queue) -> None:
        """The row's three counters are messages: scanned, filed, unkeyed."""
        await _load(state)
        await state.start_repair()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=735, failed=2, total=847)
        await queue.succeed(state.job_id)

        await state._sync_job()  # noqa: SLF001 - the panel's own read

        assert state.result_message == repaired_label(735, 847, 2)
        assert "735" in state.result_message
        assert "2 carry no Message-ID" in state.result_message

    async def test_a_running_repair_reports_nothing_yet(self, state, queue) -> None:
        """The row is written once, at the end. A sentence built from an empty
        row would say the repair filed nothing."""
        await _load(state)
        await state.start_repair()

        assert state.result_message == ""

    async def test_a_job_that_failed_before_it_counted_prints_no_zeroes(
        self, state, queue
    ) -> None:
        await _load(state)
        await state.start_repair()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.fail(state.job_id, "the graph went away mid-repair")

        await state._sync_job()  # noqa: SLF001 - the panel's own read

        assert state.result_message == ""
        assert state.job.error == "the graph went away mid-repair"


class TestFollowingTheJob:
    async def test_the_count_is_read_again_when_the_repair_ends(
        self, state, queue, archive
    ) -> None:
        """The number the panel was showing is the number the job changed."""
        await _load(state)
        await state.start_repair()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=3, failed=0, total=3)
        await queue.succeed(state.job_id)
        archive.outside = 0
        asked = archive.asks

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert archive.asks == asked + 1
        assert state.outside == 0
        assert state.outside_message == WHOLE
        assert state.polling is False
        assert state.loading is False

    async def test_a_cancelled_repair_also_refreshes_the_count(
        self, state, queue, archive
    ) -> None:
        """A run that stopped halfway still wrote the pages it reached."""
        await _load(state)
        await state.start_repair()
        await queue.request_cancel(state.job_id)
        await queue.cancel(state.job_id)
        archive.outside = 1

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.outside == 1
        assert state.job.status == "cancelled"

    async def test_a_job_that_is_gone_ends_the_follow(self, state, queue) -> None:
        """A panel left open overnight must not keep asking about a row that
        no longer exists."""
        await _load(state)
        await state.start_repair()

        with (
            _patched_loop(_forbidden_sleep()),
            patch.object(queue, "get", AsyncMock(return_value=None)),
        ):
            await _run_poll(state)

        assert state.polling is False
        assert state.job.active is False

    async def test_a_dropped_read_is_not_an_end(self, state, queue) -> None:
        """A database hiccup is not news that the repair finished."""
        await _load(state)
        await state.start_repair()
        sleep = _stopping_sleep(state)

        with (
            _patched_loop(sleep),
            patch.object(
                queue, "get", AsyncMock(side_effect=RuntimeError("db hiccup"))
            ),
        ):
            await _run_poll(state)

        assert state.job.active is True
        assert sleep.await_count == 1

    async def test_it_gives_up_on_a_job_nothing_is_running(self, state, queue) -> None:
        await _load(state)
        await state.start_repair()
        state.poll_ticks_allowed = 2
        sleep = AsyncMock()

        with _patched_loop(sleep):
            await _run_poll(state)

        assert state.polling is False
        assert state.notice == GAVE_UP

    def test_leaving_the_page_stops_the_follow(self, state) -> None:
        state.polling = True

        state.stop_polling()

        assert state.polling is False


class TestTheSentences:
    def test_one_message_is_not_pluralised(self) -> None:
        """The one time this reads ``1`` is when somebody is checking whether
        the repair worked."""
        assert outside_label(1) == "1 message sits in no conversation."

    def test_a_negative_reads_as_whole(self) -> None:
        assert outside_label(-1) == WHOLE

    def test_nothing_unkeyed_is_not_mentioned(self) -> None:
        """ "0 could not be keyed" makes a clean run look like a failed one."""
        assert "Message-ID" not in repaired_label(10, 10, 0)
