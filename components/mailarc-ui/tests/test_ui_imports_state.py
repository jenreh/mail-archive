"""Tests for :mod:`mailarc_ui.imports.state`.

Against a real SQLite file and the real :class:`JobQueue`. The state does two
things — it projects a job and it follows one — and both are only worth
anything if the counters they read are the ones the queue actually wrote; a
hand-written double would be free to agree with the projection and be wrong.

Only the clock and Reflex's own state lock are faked, so no test waits.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from appkit_commons.database.entities import Base
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mailarc_core.database.entities import MailAccountEntity, MailSyncJobEntity
from mailarc_core.database.sqlite import install_pragmas
from mailarc_sync.jobs import JobKind, JobProgress, JobQueue, SessionFactory
from mailarc_ui.imports import (
    import_controls,
    import_panel,
    import_progress,
    recent_jobs,
)
from mailarc_ui.imports.state import (
    _RECENT_LIMIT,
    ImportJobRow,
    ImportJobState,
    counts_of,
    percent_of,
)

STATE_MODULE = "mailarc_ui.imports.state"

WORKER = "worker-under-test"
LEASE_SECONDS = 60.0


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
async def account_id(session_factory: SessionFactory) -> int:
    """A stored account, because ``mail_sync_jobs.account_id`` is a real FK."""
    async with session_factory() as session:
        account = MailAccountEntity(
            provider="fake",
            display_name="Work",
            email_address="jens@example.com",
        )
        session.add(account)
        await session.flush()
        return account.id


@pytest.fixture
def state(queue: JobQueue, account_id: int) -> Iterator[ImportJobState]:
    """The state under test, with the queue it builds pointed at our file.

    The patch stays up for the whole test: the state constructs its queue
    inside every handler, which is the production path and the one worth
    exercising.
    """
    with patch(f"{STATE_MODULE}.JobQueue", Mock(return_value=queue)):
        instance = ImportJobState()
        instance.account_id = account_id
        instance.poll_interval = 0
        yield instance


async def _job_count(session_factory: SessionFactory) -> int:
    async with session_factory() as session:
        result = await session.execute(select(MailSyncJobEntity.id))
        return len(list(result.scalars().all()))


async def _run_poll(state: ImportJobState) -> None:
    """Invoke the background handler's underlying coroutine.

    Reflex refuses a direct `state.poll()` call on a background handler, so go
    through the EventHandler's wrapped function.
    """
    await ImportJobState.poll.fn(state)  # ty: ignore[unresolved-attribute]


def _stopping_sleep(state: ImportJobState, after: int = 1) -> AsyncMock:
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
        patch.object(ImportJobState, "__aenter__", AsyncMock()),
        patch.object(ImportJobState, "__aexit__", AsyncMock(return_value=False)),
    )


class TestDefaults:
    def test_nothing_is_watched_before_anything_is_started(self, state) -> None:
        assert state.job_id == 0
        assert state.job.job_id == 0
        assert state.job.percent_label == "—"
        assert state.recent == []
        assert state.has_job is False
        assert state.has_recent is False
        assert state.can_cancel is False
        assert state.polling is False

    def test_start_needs_an_account(self, state) -> None:
        assert state.can_start is True

        state.select_account(0)

        assert state.can_start is False

    def test_select_account_points_the_panel_and_clears_the_message(
        self, state
    ) -> None:
        state.message = "Choose an account first."

        state.select_account(42)

        assert state.account_id == 42
        assert state.message == ""

    def test_stop_polling_clears_the_flag(self, state) -> None:
        state.polling = True

        state.stop_polling()

        assert state.polling is False


class TestStartImport:
    async def test_enqueues_exactly_one_import_job_for_the_account(
        self, state, queue, session_factory, account_id
    ) -> None:
        result = await state.start_import()

        assert await _job_count(session_factory) == 1
        job = await queue.get(state.job_id)
        assert job is not None
        assert job.kind == JobKind.IMPORT
        assert job.account_id == account_id
        assert state.job.status == "queued"
        assert state.job.active is True
        assert state.starting is False
        assert state.polling is True
        assert result is ImportJobState.poll

    async def test_the_started_job_shows_up_in_the_history(self, state) -> None:
        await state.start_import()

        assert state.has_recent is True
        assert [row.job_id for row in state.recent] == [state.job_id]

    async def test_without_an_account_it_asks_for_one(
        self, state, session_factory
    ) -> None:
        state.select_account(0)

        assert await state.start_import() is None
        assert state.message == "Choose an account first."
        assert state.job_id == 0
        assert await _job_count(session_factory) == 0

    async def test_a_running_import_is_not_started_twice(
        self, state, session_factory
    ) -> None:
        await state.start_import()
        first = state.job_id

        assert await state.start_import() is None
        assert state.job_id == first
        assert state.message == "An import is already running."
        assert await _job_count(session_factory) == 1

    async def test_the_history_stops_at_a_handful(self, state, queue) -> None:
        """A panel is not a log — the oldest job falls off the bottom."""
        started = []
        for _ in range(_RECENT_LIMIT + 1):
            await state.start_import()
            started.append(state.job_id)
            await queue.succeed(state.job_id)
        await state.refresh()

        newest_first = list(reversed(started))
        assert [row.job_id for row in state.recent] == newest_first[:_RECENT_LIMIT]

    async def test_a_finished_import_no_longer_blocks_the_next_one(
        self, state, queue, session_factory
    ) -> None:
        """The panel may be stale; it must re-read before it refuses."""
        await state.start_import()
        first = state.job_id
        await queue.succeed(first)

        assert await state.start_import() is None  # already polling
        assert state.job_id != first
        assert await _job_count(session_factory) == 2
        assert [row.job_id for row in state.recent] == [state.job_id, first]


class TestPercentage:
    async def test_a_running_job_counts_done_and_failed_as_handled(
        self, state, queue
    ) -> None:
        await state.start_import()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=3, failed=1, total=10)

        await state.refresh()

        assert state.job.status == "running"
        assert state.job.percent == 40.0
        assert state.job.percent_label == "40%"
        assert state.job.counts_label == "3 / 10 · 1 failed"

    async def test_a_job_without_a_total_shows_no_percentage(
        self, state, queue
    ) -> None:
        """No total is not zero and even less a hundred — and never a div by 0."""
        await state.start_import()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=5, failed=0)

        await state.refresh()

        assert state.job.percent == 0.0
        assert state.job.percent_label == "—"
        assert state.job.counts_label == "5 done"

    async def test_a_finished_job_is_at_a_hundred_and_is_no_longer_active(
        self, state, queue
    ) -> None:
        await state.start_import()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=10, failed=0, total=10)
        await queue.succeed(state.job_id)

        await state.refresh()

        assert state.job.status == "succeeded"
        assert state.job.percent == 100.0
        assert state.job.percent_label == "100%"
        assert state.job.active is False
        assert state.can_cancel is False

    def test_a_total_that_shrank_under_the_counters_still_caps_at_a_hundred(
        self,
    ) -> None:
        """The provider's total is an estimate and may move while a job runs."""
        assert percent_of(JobProgress(total=4, done=6, failed=0)) == 100.0

    def test_counts_stay_quiet_when_nothing_failed(self) -> None:
        assert counts_of(JobProgress(total=10, done=2, failed=0)) == "2 / 10"


class TestCancel:
    async def test_cancel_sets_the_flag_and_the_button_follows(
        self, state, queue
    ) -> None:
        await state.start_import()
        await queue.claim(WORKER, LEASE_SECONDS)
        await state.refresh()
        assert state.can_cancel is True

        await state.cancel_import()

        assert await queue.is_cancel_requested(state.job_id) is True
        assert state.job.cancel_requested is True
        assert state.job.status == "running"  # a flag, not a kill
        assert state.can_cancel is False
        assert state.cancelling is False
        assert state.message == ""

    async def test_cancelling_a_job_that_already_ended_says_so(
        self, state, queue
    ) -> None:
        await state.start_import()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.succeed(state.job_id)

        await state.cancel_import()

        assert state.message == "That job had already ended."
        assert state.can_cancel is False

    async def test_cancel_without_a_job_is_a_no_op(self, state) -> None:
        await state.cancel_import()

        assert state.message == ""
        assert state.job.job_id == 0


class TestPolling:
    async def test_the_poll_picks_up_changed_counters(self, state, queue) -> None:
        await state.start_import()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=3, failed=0, total=10)
        sleep = _stopping_sleep(state)

        with _patched_loop(sleep):
            await _run_poll(state)

        assert state.job.status == "running"
        assert state.job.percent == 30.0
        assert state.job.counts_label == "3 / 10"
        assert sleep.await_count == 1

    async def test_a_finished_job_stops_the_poll_instead_of_spinning(
        self, state, queue
    ) -> None:
        await state.start_import()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.succeed(state.job_id)

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.polling is False
        assert state.job.status == "succeeded"

    async def test_the_poll_ends_when_nothing_is_being_watched(self, state) -> None:
        state.polling = True

        with _patched_loop(_forbidden_sleep()):
            await _run_poll(state)

        assert state.polling is False

    async def test_a_failing_read_does_not_end_the_poll(self, state, queue) -> None:
        """A dropped read is a hiccup; the next tick asks again."""
        await state.start_import()
        job = await queue.get(state.job_id)
        sleep = _stopping_sleep(state)

        with (
            patch.object(
                queue, "get", AsyncMock(side_effect=[RuntimeError("db went away"), job])
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
        await state.start_import()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(state.job_id, done=3, failed=0, total=10)
        real_get = queue.get

        async def stop_then_answer(job_id: int):
            state.polling = False
            return await real_get(job_id)

        with (
            patch.object(queue, "get", AsyncMock(side_effect=stop_then_answer)),
            _patched_loop(_forbidden_sleep()),
        ):
            await _run_poll(state)

        assert state.job.percent == 0.0
        assert state.job.counts_label == "0 done"

    async def test_a_job_that_vanished_drops_out_of_the_history(
        self, state, session_factory
    ) -> None:
        """A deleted account takes its jobs with it; the panel must not care."""
        await state.start_import()
        async with session_factory() as session:
            await session.execute(
                delete(MailSyncJobEntity).where(MailSyncJobEntity.id == state.job_id)
            )

        await state.refresh()

        assert state.recent == []
        assert state.job.job_id == 0
        assert state.has_job is False


class TestComponents:
    """Rendering is the only way to catch a prop appkit_mantine does not have."""

    @pytest.mark.parametrize(
        "factory", [import_controls, import_progress, recent_jobs, import_panel]
    )
    def test_it_builds_and_renders(self, factory) -> None:
        assert factory().render()


class TestProjection:
    async def test_what_reaches_the_browser_is_the_row_and_nothing_else(
        self, state, queue, account_id
    ) -> None:
        """§9.1: a small projection, never the ORM entity or the rich job."""
        await state.start_import()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.fail(state.job_id, "the mailbox refused us")
        await state.refresh()

        assert state.recent[0].model_dump() == {
            "job_id": state.job_id,
            "account_id": account_id,
            "status": "failed",
            "status_color": "red",
            "percent": 0.0,
            "percent_label": "—",
            "counts_label": "0 done",
            "error": "the mailbox refused us",
            "active": False,
            "cancel_requested": False,
        }

    def test_a_row_cannot_be_edited_once_read(self) -> None:
        row = ImportJobRow(job_id=1)

        with pytest.raises(ValidationError):
            row.status = "tampered"  # ty: ignore[invalid-assignment]
