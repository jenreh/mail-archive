"""Tests for :class:`mailarc_sync.jobs.queue.JobQueue`.

Against a real SQLite file, never a fake. The whole point of this module is a
conditional ``UPDATE`` and what its ``rowcount`` says under concurrency — the
one thing a hand-written double would be free to get wrong.

The session factory here mirrors appkit's ``AsyncSessionManager``: commit on a
clean exit, roll back on an exception. That is the contract ``JobQueue``
assumes of ``get_asyncdb_session`` in production.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from appkit_commons.database.entities import Base
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mailarc_core.database.entities import MailAccountEntity, MailSyncJobEntity
from mailarc_core.database.repositories import SyncJobRepository
from mailarc_core.database.sqlite import install_pragmas
from mailarc_sync.jobs.model import JobKind, JobState
from mailarc_sync.jobs.queue import JobQueue, SessionFactory


@pytest.fixture
async def engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    """A fresh database file with the mail tables on it.

    The pragmas are the production ones: WAL and ``busy_timeout`` are what let
    two claims race without one of them hearing "database is locked".
    """
    install_pragmas()
    created = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail-archive.db'}")
    async with created.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> SessionFactory:
    """One session per call, committed on the way out."""
    return factory_for(engine)


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


def factory_for(engine: AsyncEngine) -> SessionFactory:
    """Build a session factory with appkit's transaction semantics."""
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


async def expire_lease(session_factory: SessionFactory, job_id: int) -> None:
    """Stand in for a ``kill -9``: the lease stops moving and falls behind."""
    async with session_factory() as session:
        job = await session.get(MailSyncJobEntity, job_id)
        assert job is not None
        job.lease_until = datetime.now(UTC) - timedelta(minutes=5)


class TestEnqueue:
    async def test_a_queued_job_comes_back_with_its_kind_and_account(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)

        job = await queue.get(job_id)

        assert job is not None
        assert job.state is JobState.QUEUED
        assert job.kind is JobKind.IMPORT
        assert job.account_id == account_id
        assert job.worker_id is None

    async def test_a_job_without_an_account_is_allowed(self, queue: JobQueue) -> None:
        # `derive` and `embed` work on the whole archive, not one mailbox.
        job_id = await queue.enqueue(JobKind.DERIVE)

        job = await queue.get(job_id)

        assert job is not None
        assert job.account_id is None

    async def test_an_unknown_id_reads_as_nothing(self, queue: JobQueue) -> None:
        assert await queue.get(4711) is None


class TestClaim:
    async def test_claiming_moves_the_job_to_running_under_a_lease(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)

        claimed = await queue.claim("worker-a", 60)

        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.state is JobState.RUNNING
        assert claimed.worker_id == "worker-a"
        assert claimed.lease_until is not None
        assert claimed.started_at is not None

    async def test_an_empty_queue_hands_out_nothing(self, queue: JobQueue) -> None:
        assert await queue.claim("worker-a", 60) is None

    async def test_a_running_job_is_not_handed_out_twice(
        self, queue: JobQueue, account_id: int
    ) -> None:
        await queue.enqueue(JobKind.IMPORT, account_id)
        assert await queue.claim("worker-a", 60) is not None

        assert await queue.claim("worker-b", 60) is None

    async def test_the_oldest_queued_job_goes_first(
        self, queue: JobQueue, account_id: int
    ) -> None:
        first = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.enqueue(JobKind.INCREMENTAL, account_id)

        claimed = await queue.claim("worker-a", 60)

        assert claimed is not None
        assert claimed.id == first

    async def test_two_concurrent_claims_produce_exactly_one_winner(
        self, queue: JobQueue, account_id: int
    ) -> None:
        # The compare-and-swap standing in for SKIP LOCKED. Both workers see
        # the same candidate; only one UPDATE may match a row.
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)

        first, second = await asyncio.gather(
            queue.claim("worker-a", 60), queue.claim("worker-b", 60)
        )

        winners = [claim for claim in (first, second) if claim is not None]
        assert len(winners) == 1
        assert winners[0].id == job_id

        job = await queue.get(job_id)
        assert job is not None
        assert job.worker_id == winners[0].worker_id

    async def test_many_concurrent_claims_never_hand_one_job_out_twice(
        self, queue: JobQueue, account_id: int
    ) -> None:
        for _ in range(3):
            await queue.enqueue(JobKind.IMPORT, account_id)

        claims = await asyncio.gather(
            *(queue.claim(f"worker-{index}", 60) for index in range(6))
        )

        taken = [claim.id for claim in claims if claim is not None]
        assert sorted(taken) == sorted(set(taken))
        assert len(taken) == 3


class TestHeartbeat:
    async def test_a_heartbeat_pushes_the_lease_out(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        claimed = await queue.claim("worker-a", 1)
        assert claimed is not None

        assert await queue.heartbeat(job_id, 600) is True

        job = await queue.get(job_id)
        assert job is not None
        assert claimed.lease_until is not None
        assert job.lease_until is not None
        assert job.lease_until > claimed.lease_until
        assert job.heartbeat_at is not None

    async def test_a_heartbeat_on_a_job_that_is_not_running_is_refused(
        self, queue: JobQueue, account_id: int
    ) -> None:
        # This is how a worker learns it lost its job.
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)

        assert await queue.heartbeat(job_id, 60) is False


class TestReclaim:
    async def test_an_expired_lease_goes_back_in_the_queue(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.claim("worker-a", 60)
        await expire_lease(session_factory, job_id)

        assert await queue.reclaim_expired() == 1

        job = await queue.get(job_id)
        assert job is not None
        assert job.state is JobState.QUEUED
        assert job.worker_id is None
        assert job.lease_until is None

    async def test_a_live_lease_is_left_alone(
        self, queue: JobQueue, account_id: int
    ) -> None:
        await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.claim("worker-a", 600)

        assert await queue.reclaim_expired() == 0

    async def test_a_reclaimed_job_can_be_claimed_again(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.claim("worker-a", 60)
        await expire_lease(session_factory, job_id)
        await queue.reclaim_expired()

        taken = await queue.claim("worker-b", 60)

        assert taken is not None
        assert taken.id == job_id
        assert taken.worker_id == "worker-b"

    async def test_a_resumed_job_keeps_the_time_its_work_began(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        first = await queue.claim("worker-a", 60)
        assert first is not None
        await expire_lease(session_factory, job_id)
        await queue.reclaim_expired()

        second = await queue.claim("worker-b", 60)

        assert second is not None
        assert second.started_at == first.started_at


class TestProgress:
    async def test_progress_is_recorded(self, queue: JobQueue, account_id: int) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.claim("worker-a", 60)

        assert await queue.progress(job_id, done=12, failed=1, total=40)

        job = await queue.get(job_id)
        assert job is not None
        assert job.progress.done == 12
        assert job.progress.failed == 1
        assert job.progress.total == 40

    async def test_leaving_the_total_out_keeps_the_estimate(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.claim("worker-a", 60)
        await queue.progress(job_id, done=0, failed=0, total=40)

        await queue.progress(job_id, done=20, failed=0)

        job = await queue.get(job_id)
        assert job is not None
        assert job.progress.total == 40

    async def test_a_job_that_is_not_running_takes_no_progress(
        self, queue: JobQueue, account_id: int
    ) -> None:
        """A lease that expired mid-page leaves its old owner still counting.

        Without the guard that worker's last report would land on top of the
        numbers the worker which took the job over is producing, and the
        progress bar would walk backwards.
        """
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.claim("worker-a", 60)
        await queue.progress(job_id, done=20, failed=0, total=40)
        await queue.succeed(job_id)

        assert not await queue.progress(job_id, done=3, failed=0)

        job = await queue.get(job_id)
        assert job is not None
        assert job.progress.done == 20, "the finished tally stands"

    async def test_a_queued_job_takes_no_progress(
        self, queue: JobQueue, account_id: int
    ) -> None:
        """Nobody is working on it, so nobody has a tally to report."""
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)

        assert not await queue.progress(job_id, done=7, failed=0)

        job = await queue.get(job_id)
        assert job is not None
        assert job.progress.done == 0


class TestEndStates:
    async def test_success_lets_go_of_the_lease(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.claim("worker-a", 60)

        await queue.succeed(job_id)

        job = await queue.get(job_id)
        assert job is not None
        assert job.state is JobState.SUCCEEDED
        assert job.finished_at is not None
        assert job.worker_id is None
        assert job.lease_until is None
        assert job.error is None

    async def test_failure_keeps_the_reason(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)

        await queue.fail(job_id, "auth: the token was revoked")

        job = await queue.get(job_id)
        assert job is not None
        assert job.state is JobState.FAILED
        assert job.error == "auth: the token was revoked"

    async def test_a_finished_job_is_not_claimed_again(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.succeed(job_id)

        assert await queue.claim("worker-a", 60) is None


class TestCancel:
    async def test_a_cancel_request_is_a_flag_not_an_end_state(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.claim("worker-a", 60)

        assert await queue.request_cancel(job_id) is True

        job = await queue.get(job_id)
        assert job is not None
        assert job.cancel_requested is True
        assert job.state is JobState.RUNNING

    async def test_the_flag_is_readable_on_its_own(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)

        assert await queue.is_cancel_requested(job_id) is False
        await queue.request_cancel(job_id)
        assert await queue.is_cancel_requested(job_id) is True

    async def test_a_finished_job_cannot_be_asked_to_stop(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.succeed(job_id)

        assert await queue.request_cancel(job_id) is False

    async def test_cancelling_ends_the_job(
        self, queue: JobQueue, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        await queue.claim("worker-a", 60)
        await queue.request_cancel(job_id)

        await queue.cancel(job_id)

        job = await queue.get(job_id)
        assert job is not None
        assert job.state is JobState.CANCELLED
        assert job.finished_at is not None


class TestRepositoryInterop:
    async def test_a_job_enqueued_through_the_repository_is_claimable(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        # The UI enqueues through the repository API; the queue must not care.
        async with session_factory() as session:
            created = await SyncJobRepository().create(
                session,
                MailSyncJobEntity(kind=JobKind.IMPORT, account_id=account_id),
            )
            job_id = created.id

        claimed = await queue.claim("worker-a", 60)

        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.kind is JobKind.IMPORT
