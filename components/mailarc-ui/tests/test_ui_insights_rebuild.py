"""Tests for the rebuild control in :mod:`mailarc_ui.insights.state`.

Against the real :class:`JobQueue` on a real SQLite file — the state projects a
job and it follows one, and both are only worth anything if the counters it
reads are the ones the queue actually wrote. Only the clock and Reflex's own
state lock are faked, so no test waits.

The panels are read through the same fake archive the reading tests use,
because the claim these tests exist for is that a rebuild *ending* is what
refreshes them: succeeded, failed and cancelled alike, since a rebuild that
stopped halfway leaves a derived layer that no longer matches what the page is
showing.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from appkit_commons.database.entities import Base
from insights_archive import fresh, graph, published
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mailarc_analytics import AnalyticsReader
from mailarc_analytics.queries import catalog
from mailarc_core.database.entities import MailSyncJobEntity
from mailarc_core.database.sqlite import install_pragmas
from mailarc_sync.jobs import JobKind, JobQueue, SessionFactory
from mailarc_ui.insights import AnalyticsInsightsState

__all__ = ["fresh", "graph", "published"]
"""pytest collects a fixture off the importing module's namespace, so the three
are imported to be used; ``__all__`` is what stops ruff removing them again."""

STATE_MODULE = "mailarc_ui.insights.state"

WORKER = "worker-under-test"
LEASE_SECONDS = 60.0


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
def state(
    published: AnalyticsReader, queue: JobQueue
) -> Iterator[AnalyticsInsightsState]:
    """The state under test, with the queue it builds pointed at our file.

    The patch stays up for the whole test: the state constructs its queue
    inside every handler, which is the production path.
    """
    with patch(f"{STATE_MODULE}.JobQueue", Mock(return_value=queue)):
        instance = AnalyticsInsightsState()
        instance.poll_interval = 0
        yield instance


async def _run_poll(state: AnalyticsInsightsState) -> None:
    """Invoke the background handler's underlying coroutine.

    Reflex refuses a direct call on a background handler, so go through the
    EventHandler's wrapped function.
    """
    await AnalyticsInsightsState.poll.fn(state)


def _stopping_sleep(state: AnalyticsInsightsState, after: int = 1) -> AsyncMock:
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
    """Fake the clock and the state lock the background task takes.

    The same three patches the import panel's tests need, and copied rather
    than shared: a helper that two test modules reach into is a third place to
    keep in step with Reflex.
    """
    return _MultiPatch(
        patch.object(asyncio, "sleep", sleep),
        patch.object(AnalyticsInsightsState, "__aenter__", AsyncMock()),
        patch.object(
            AnalyticsInsightsState, "__aexit__", AsyncMock(return_value=False)
        ),
    )


async def _load(state: AnalyticsInsightsState) -> None:
    """Invoke the page's ``on_load`` the way Reflex invokes a background task.

    ``load`` reads the whole archive and holds the state lock only around its
    two mutations, so Reflex refuses a direct call on it; going through the
    ``EventHandler``'s wrapped function is the same code the app runs.
    """
    await AnalyticsInsightsState.load.fn(state)


async def _check_agreement(state: AnalyticsInsightsState) -> None:
    """The Cross-check button, same reason as :func:`_load`."""
    await AnalyticsInsightsState.check_agreement.fn(state)


class TestRebuilding:
    async def test_a_rebuild_is_one_derive_job_about_the_whole_archive(
        self, state, queue
    ) -> None:
        result = await state.start_rebuild()

        job = await queue.get(state.job_id)
        assert job is not None
        assert job.kind == JobKind.DERIVE
        assert job.account_id is None
        assert state.job.status == "queued"
        assert state.job.active is True
        assert state.can_rebuild is False
        assert state.polling is True
        assert result is AnalyticsInsightsState.poll

    async def test_a_running_rebuild_is_not_started_twice(self, state) -> None:
        await state.start_rebuild()
        first = state.job_id

        assert await state.start_rebuild() is None
        assert state.job_id == first
        assert state.rebuild_message == "A rebuild is already running."

    async def test_a_finished_rebuild_no_longer_blocks_the_next_one(
        self, state, queue
    ) -> None:
        """The panel may be stale; it must re-read before it refuses."""
        await state.start_rebuild()
        first = state.job_id
        await queue.succeed(first)

        assert await state.start_rebuild() is None  # already polling
        assert state.job_id != first
        assert state.job.active is True

    async def test_a_queue_that_refuses_leaves_a_message_not_a_traceback(
        self, state, queue
    ) -> None:
        with patch.object(
            queue, "enqueue", AsyncMock(side_effect=RuntimeError("db went away"))
        ):
            assert await state.start_rebuild() is None

        assert "db went away" in state.rebuild_message
        assert state.starting is False
        assert state.polling is False

    async def test_the_bar_counts_stages_and_not_messages(self, state, queue) -> None:
        """The worker moves the row on once per analysis; a percentage of the
        archive would be a nicer number and a made-up one."""
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=3, failed=0, total=7)

        await _load(state)
        await state._sync_job()  # noqa: SLF001 - the panel's own read

        assert state.job.status == "running"
        assert state.job.percent_label == "43%"
        assert state.job.stages_label == "3 of 7 stages"

    async def test_a_rebuild_that_has_not_reported_yet_shows_no_percentage(
        self, state
    ) -> None:
        await state.start_rebuild()

        assert state.job.percent_label == "—"
        assert state.job.stages_label == ""

    async def test_cancel_asks_and_the_button_follows(self, state, queue) -> None:
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        await state._sync_job()  # noqa: SLF001 - the panel's own read
        assert state.can_cancel is True

        await state.cancel_rebuild()

        assert await queue.is_cancel_requested(state.job_id) is True
        assert state.job.cancel_requested is True
        assert state.job.status == "running"  # a flag, not a kill
        assert state.can_cancel is False
        assert state.cancelling is False

    async def test_cancelling_a_rebuild_that_already_ended_says_so(
        self, state, queue
    ) -> None:
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.succeed(state.job_id)

        await state.cancel_rebuild()

        assert state.rebuild_message == "That rebuild had already ended."

    async def test_cancel_without_a_rebuild_is_a_no_op(self, state) -> None:
        await state.cancel_rebuild()

        assert state.rebuild_message == ""
        assert state.has_job is False

    async def test_a_read_that_fails_leaves_the_last_reading_standing(
        self, state, queue
    ) -> None:
        """A database hiccup is not news that the rebuild ended, and a button
        handler is not a place to raise from."""
        await state.start_rebuild()
        watched = state.job_id

        with patch.object(
            queue, "get", AsyncMock(side_effect=RuntimeError("db went away"))
        ):
            assert await state.start_rebuild() is None

        assert state.job_id == watched
        assert state.job.active is True
        assert state.rebuild_message == "A rebuild is already running."

    def test_stop_polling_clears_the_flag(self, state) -> None:
        state.polling = True

        state.stop_polling()

        assert state.polling is False


class TestWhenNoWorkerIsRunning:
    """The normal state of a dev machine, and of any install without a worker.

    Everything below used to end in a control that says a rebuild is running,
    shows two disabled buttons and explains nothing — with the only way out
    being a page reload that queues a second job behind the first.
    """

    async def test_cancelling_before_a_worker_arrives_gives_the_control_back(
        self, state, queue
    ) -> None:
        """A flag needs a reader, and a queued job has none.

        This used to leave the job QUEUED and active with the flag set, which
        disabled *both* buttons and wrote no message: a control that claimed a
        rebuild was running, offered nothing, and explained nothing. The only
        way out was a reload, which queued a second job.
        """
        await state.start_rebuild()

        await state.cancel_rebuild()

        assert state.job.status == "cancelled"
        assert state.can_rebuild is True
        assert state.can_cancel is False
        assert "before any worker picked it up" in state.rebuild_message

    async def test_cancelling_a_rebuild_a_worker_holds_still_only_asks(
        self, state, queue
    ) -> None:
        """The other half of the same button, and the reason for the flag: a
        stage that is half written gets to finish being written."""
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        await state._sync_job()  # noqa: SLF001 - the panel's own read

        await state.cancel_rebuild()

        assert state.job.status == "running"
        assert state.can_rebuild is False
        assert "Cancellation requested" in state.rebuild_message

    async def test_a_reloaded_page_follows_the_rebuild_instead_of_queueing(
        self, state, queue, published
    ) -> None:
        """Two open pages, or one page after a reload, used to queue two.

        The guard was ``self.job.active`` and ``job_id`` starts at 0, so a
        freshly loaded page knew about no rebuild and enqueued its own. A
        derive job begins by deleting the derived layer, so two of them
        interleaving can wipe rows the other has already written.
        """
        await state.start_rebuild()
        first = state.job_id

        with patch(f"{STATE_MODULE}.JobQueue", Mock(return_value=queue)):
            second_page = AnalyticsInsightsState()
            second_page.poll_interval = 0
            assert await second_page.start_rebuild() is not None

        assert second_page.job_id == first
        assert second_page.rebuild_message == "A rebuild is already running."
        assert await _job_count(queue) == 1

    async def test_the_poll_gives_up_on_a_rebuild_nobody_picks_up(
        self, state, queue
    ) -> None:
        """A loop whose only exit is a final state never ends without a worker.

        Verified against the real queue before the bound existed: the loop
        ticked indefinitely against a QUEUED job, and ``stop_polling`` — the
        one thing that could have ended it — had no caller anywhere in the
        repository. A page left open kept hitting the database every two
        seconds for the life of the session.
        """
        await state.start_rebuild()
        state.poll_ticks_allowed = 3
        sleep = AsyncMock()

        with _patched_loop(sleep):
            await _run_poll(state)

        assert state.polling is False
        assert state.job.status == "queued"
        assert sleep.await_count == 3
        assert "no worker" in state.rebuild_message

    async def test_a_rebuild_that_is_running_gets_a_different_sentence(
        self, state, queue
    ) -> None:
        """Giving up is a statement about this page, never about the job.

        A rebuild a worker really is running has not gone wrong; the page has
        simply stopped following it, and telling that reader to start a worker
        would be wrong twice over.
        """
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        state.poll_ticks_allowed = 1

        with _patched_loop(AsyncMock()):
            await _run_poll(state)

        assert state.job.status == "running"
        assert "no worker" not in state.rebuild_message
        assert "Refresh to see where it got to" in state.rebuild_message

    async def test_a_queue_that_cannot_be_asked_still_queues_the_rebuild(
        self, state, queue
    ) -> None:
        """Refusing to start because the *lookup* failed would be the wrong
        way to fail: the enqueue immediately after goes to the same database
        and already has the message for it."""
        with patch.object(
            queue, "find_open", AsyncMock(side_effect=RuntimeError("db went away"))
        ):
            assert await state.start_rebuild() is not None

        assert state.job_id > 0
        assert state.job.active is True


async def _job_count(queue: JobQueue) -> int:
    """How many rows the queue holds, asked the way a test may ask it."""
    async with queue._session_factory() as session:  # noqa: SLF001 - a test's reach
        result = await session.execute(select(MailSyncJobEntity.id))
        return len(result.scalars().all())


class TestPolling:
    async def test_the_poll_picks_up_a_stage_that_finished(self, state, queue) -> None:
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=2, failed=0, total=7)
        sleep = _stopping_sleep(state)

        with _patched_loop(sleep):
            await _run_poll(state)

        assert state.job.stages_label == "2 of 7 stages"
        assert sleep.await_count == 1

    async def test_a_finished_rebuild_stops_the_poll_and_reads_the_panels(
        self, state, queue, graph
    ) -> None:
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.succeed(state.job_id)

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.polling is False
        assert state.job.status == "succeeded"
        assert state.totals.messages == 12
        assert len(state.groups) == 2
        assert state.agreement.agrees is True
        assert state.busy is False
        assert catalog.CO_RECIPIENTS in graph.asked

    async def test_a_failed_rebuild_reads_the_panels_too(self, state, queue) -> None:
        """A rebuild that stopped halfway leaves half a derived layer, and the
        page must not keep showing the one from before it."""
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.fail(state.job_id, "the graph refused us")

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.polling is False
        assert state.job.status == "failed"
        assert state.job.error == "the graph refused us"
        assert state.totals.messages == 12

    async def test_a_cancelled_rebuild_reads_the_panels_too(self, state, queue) -> None:
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.cancel(state.job_id)

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.polling is False
        assert state.job.status == "cancelled"
        assert state.job.status_color == "orange"
        assert state.totals.messages == 12

    async def test_the_poll_ends_when_nothing_is_being_watched(self, state) -> None:
        state.polling = True

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.polling is False

    async def test_a_rebuild_that_vanished_ends_the_poll(
        self, state, session_factory
    ) -> None:
        """A deleted row is not a job that is still running."""
        await state.start_rebuild()
        async with session_factory() as session:
            await session.execute(
                delete(MailSyncJobEntity).where(MailSyncJobEntity.id == state.job_id)
            )

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.polling is False
        assert state.has_job is False

    async def test_a_dropped_read_does_not_end_the_poll(self, state, queue) -> None:
        """A hiccup is a hiccup; the next tick asks again."""
        await state.start_rebuild()
        job = await queue.get(state.job_id)
        sleep = _stopping_sleep(state)

        with (
            patch.object(
                queue, "get", AsyncMock(side_effect=[RuntimeError("gone"), job])
            ),
            _patched_loop(sleep),
        ):
            await _run_poll(state)

        assert sleep.await_count == 1
        assert state.polling is False

    async def test_a_reading_that_arrives_after_stop_is_discarded(
        self, state, queue
    ) -> None:
        """Once the panel is off, nothing may mutate it behind the scenes."""
        await state.start_rebuild()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=4, failed=0, total=7)
        real_get = queue.get

        async def stop_then_answer(job_id: int):
            state.polling = False
            return await real_get(job_id)

        with (
            patch.object(queue, "get", AsyncMock(side_effect=stop_then_answer)),
            _patched_loop(_forbidden_sleep()),
        ):
            await _run_poll(state)

        assert state.job.stages_label == ""
