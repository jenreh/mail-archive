"""Tests for :class:`mailarc_sync.jobs.worker.JobWorker`.

The three Phase 2 promises are tested against a real queue on a real SQLite
file, with a handler that behaves like an import: it walks a fixed list of
messages in batches, writes a checkpoint after each one, and asks between
batches whether it should stop.

The crash is simulated by writing ``lease_until`` into the past rather than by
killing a process — the queue cannot tell the difference, and a test that kills
its own runner proves less, not more.
"""

import asyncio
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from appkit_commons.database.entities import Base
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mailarc_core.database.entities import (
    AccountStatus,
    MailAccountEntity,
    MailFailedMessageEntity,
    MailSyncJobEntity,
)
from mailarc_core.database.repositories import (
    SyncCheckpointRepository,
    SyncJobRepository,
)
from mailarc_core.database.sqlite import install_pragmas
from mailarc_core.mail.errors import (
    MailAuthError,
    MailPermanentError,
    MailTransientError,
)
from mailarc_sync.jobs.model import JobKind, JobState, SyncJob
from mailarc_sync.jobs.queue import JobQueue, SessionFactory
from mailarc_sync.jobs.worker import JobHandler, JobWorker, default_worker_id

MESSAGES = ("m1", "m2", "m3", "m4", "m5", "m6")
"""The mailbox the fake import walks."""

BATCH_SIZE = 2
"""Small enough that a cancel has two batches to land between."""

SCOPE = "all"
"""One checkpoint per account is all this fixture mailbox needs."""

TERMINAL = (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


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
    """Appkit's transaction semantics: commit on a clean exit, else roll back."""
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
    async with session_factory() as session:
        account = MailAccountEntity(
            provider="fake",
            display_name="Work",
            email_address="jens@example.com",
        )
        session.add(account)
        await session.flush()
        return account.id


def worker_for(
    queue: JobQueue,
    session_factory: SessionFactory,
    handlers: dict[JobKind, JobHandler],
    **overrides: object,
) -> JobWorker:
    """A worker tuned for a test: fast polling, no global signal handlers."""
    settings: dict[str, object] = {
        "worker_id": "worker-under-test",
        "session_factory": session_factory,
        "poll_seconds": 0.01,
        "heartbeat_seconds": 0.01,
        "backoff_seconds": 0.001,
        "max_backoff_seconds": 0.01,
        "handle_signals": False,
    }
    settings.update(overrides)
    return JobWorker(queue, handlers, **settings)  # type: ignore[arg-type]


def importing(session_factory: SessionFactory, processed: list[str]) -> JobHandler:
    """A handler shaped like the real import.

    Resumes from the checkpoint, writes a batch at a time, and reads the cancel
    flag only between batches — the three things the DoD asks about.
    """
    checkpoints = SyncCheckpointRepository()

    async def handle(job: SyncJob, queue: JobQueue) -> None:
        assert job.account_id is not None
        start = await seen_so_far(session_factory, job.account_id)
        for offset in range(start, len(MESSAGES), BATCH_SIZE):
            if await queue.is_cancel_requested(job.id):
                return
            processed.extend(MESSAGES[offset : offset + BATCH_SIZE])
            done = min(offset + BATCH_SIZE, len(MESSAGES))
            async with session_factory() as session:
                await checkpoints.upsert_cursor(
                    session, job.account_id, SCOPE, str(done), done
                )
            await queue.progress(job.id, done=done, failed=0, total=len(MESSAGES))

    return handle


async def seen_so_far(session_factory: SessionFactory, account_id: int) -> int:
    """How far the last run got, or zero if there was none."""
    async with session_factory() as session:
        checkpoint = await SyncCheckpointRepository().find_by_account_and_scope(
            session, account_id, SCOPE
        )
        return 0 if checkpoint is None else checkpoint.messages_seen


async def expire_lease(session_factory: SessionFactory, job_id: int) -> None:
    """Stand in for a ``kill -9``: the lease stops moving and falls behind."""
    async with session_factory() as session:
        job = await session.get(MailSyncJobEntity, job_id)
        assert job is not None
        job.lease_until = datetime.now(UTC) - timedelta(minutes=5)


async def wait_for_state(
    queue: JobQueue,
    job_id: int,
    states: tuple[JobState, ...] = TERMINAL,
    timeout: float = 5.0,
) -> SyncJob:
    """Poll until the job reaches one of ``states``."""

    async def poll() -> SyncJob:
        while True:
            job = await queue.get(job_id)
            if job is not None and job.state in states:
                return job
            await asyncio.sleep(0.005)

    return await asyncio.wait_for(poll(), timeout)


async def run_until_done(
    worker: JobWorker, queue: JobQueue, job_id: int, timeout: float = 5.0
) -> SyncJob:
    """Start the loop, wait for the job to end, stop the loop again."""
    loop_task = asyncio.create_task(worker.run())
    try:
        return await wait_for_state(queue, job_id, TERMINAL, timeout)
    finally:
        worker.request_stop()
        await asyncio.wait_for(loop_task, timeout)


class TestPickingUpWork:
    async def test_a_job_enqueued_through_the_repository_is_picked_up_and_run(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        # Phase 2 DoD, first promise. The UI enqueues through the repository;
        # nothing tells the worker, it finds the row on its next poll.
        processed: list[str] = []
        async with session_factory() as session:
            created = await SyncJobRepository().create(
                session,
                MailSyncJobEntity(kind=JobKind.IMPORT, account_id=account_id),
            )
            job_id = created.id

        worker = worker_for(
            queue,
            session_factory,
            {JobKind.IMPORT: importing(session_factory, processed)},
        )
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.SUCCEEDED
        assert processed == list(MESSAGES)
        assert job.progress.done == len(MESSAGES)
        assert job.progress.total == len(MESSAGES)

    async def test_a_kind_without_a_handler_fails_the_job_instead_of_the_worker(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        job_id = await queue.enqueue(JobKind.DERIVE, account_id)

        worker = worker_for(queue, session_factory, {JobKind.IMPORT: never_called})
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.FAILED
        assert job.error is not None
        assert "no handler" in job.error

    async def test_the_loop_stops_when_asked_while_idle(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        worker = worker_for(queue, session_factory, {}, poll_seconds=30.0)

        loop_task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.02)
        worker.request_stop()

        # Promptly, not after the poll interval: the wait races the flag.
        await asyncio.wait_for(loop_task, 1.0)

    async def test_a_worker_names_itself_by_process_and_host(self) -> None:
        assert "@" in default_worker_id()
        assert len(default_worker_id()) <= 64

    async def test_a_worker_says_which_name_it_holds_leases_under(
        self, queue: JobQueue
    ) -> None:
        worker = JobWorker(queue, {}, worker_id="worker-7", handle_signals=False)

        assert worker.worker_id == "worker-7"


class TestResumingAfterACrash:
    async def test_an_expired_lease_is_reclaimed_and_resumes_without_duplicates(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        # Phase 2 DoD, second promise. A worker gets one batch done and dies;
        # the next start takes the job over and carries on where it stopped.
        processed: list[str] = []
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)

        claimed = await queue.claim("worker-that-dies", 60)
        assert claimed is not None
        processed.extend(MESSAGES[:BATCH_SIZE])
        async with session_factory() as session:
            await SyncCheckpointRepository().upsert_cursor(
                session, account_id, SCOPE, str(BATCH_SIZE), BATCH_SIZE
            )
        await expire_lease(session_factory, job_id)

        worker = worker_for(
            queue,
            session_factory,
            {JobKind.IMPORT: importing(session_factory, processed)},
        )
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.SUCCEEDED
        # Every message exactly once, in order: the first batch was not redone.
        assert processed == list(MESSAGES)
        assert job.worker_id is None

    async def test_a_live_lease_is_not_stolen_from_the_worker_holding_it(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        assert await queue.claim("worker-elsewhere", 600) is not None

        worker = worker_for(queue, session_factory, {JobKind.IMPORT: never_called})
        loop_task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        worker.request_stop()
        await asyncio.wait_for(loop_task, 1.0)

        job = await queue.get(job_id)
        assert job is not None
        assert job.state is JobState.RUNNING
        assert job.worker_id == "worker-elsewhere"

    async def test_the_lease_is_pushed_out_while_a_handler_works(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        release = asyncio.Event()

        async def slow(job: SyncJob, queue_: JobQueue) -> None:
            await release.wait()

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(queue, session_factory, {JobKind.IMPORT: slow})
        loop_task = asyncio.create_task(worker.run())
        try:
            running = await wait_for_state(queue, job_id, (JobState.RUNNING,))
            assert running.lease_until is not None
            beaten = await wait_for_lease_beyond(queue, job_id, running.lease_until)
            assert beaten.heartbeat_at is not None
        finally:
            release.set()
            worker.request_stop()
            await asyncio.wait_for(loop_task, 5.0)

    async def test_the_heartbeat_gives_up_once_the_job_is_no_longer_ours(
        self,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        # A worker whose lease ran out must not push out the lease of the one
        # that took over. The refused heartbeat is how it finds out.
        counting = CountingQueue(session_factory)
        release = asyncio.Event()

        async def slow(job: SyncJob, queue_: JobQueue) -> None:
            await release.wait()

        job_id = await counting.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(counting, session_factory, {JobKind.IMPORT: slow})
        loop_task = asyncio.create_task(worker.run())
        try:
            await wait_for_state(counting, job_id, (JobState.RUNNING,))
            await wait_for_heartbeats(counting, at_least=2)

            # Somebody else ended the job underneath us.
            await counting.fail(job_id, "ended from elsewhere")
            await asyncio.sleep(0.05)
            settled = counting.heartbeats
            await asyncio.sleep(0.1)

            assert counting.heartbeats == settled
        finally:
            release.set()
            worker.request_stop()
            await asyncio.wait_for(loop_task, 5.0)

    async def test_losing_the_job_calls_off_the_work_as_well(
        self,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        # The lease is what says the job is ours. A handler that keeps running
        # after it is gone imports a mailbox the worker that took over is
        # already importing, and then writes its own outcome onto that
        # worker's job.
        losing = LosingQueue(session_factory)
        finished: list[str] = []

        async def slow(job: SyncJob, queue_: JobQueue) -> None:
            # Runs until the lease is refused, then keeps going — the window
            # in which a worker that has lost its job does damage.
            await asyncio.wait_for(losing.refused.wait(), 5.0)
            await asyncio.sleep(0.25)
            finished.append("handler ran to the end")

        job_id = await losing.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(losing, session_factory, {JobKind.IMPORT: slow})
        loop_task = asyncio.create_task(worker.run())
        try:
            await wait_for_state(losing, job_id, (JobState.RUNNING,))
            await wait_for_heartbeats(losing, at_least=1)

            # Somebody else ended the job underneath us.
            await losing.fail(job_id, "ended from elsewhere")
            await asyncio.wait_for(losing.refused.wait(), 5.0)
            await asyncio.sleep(0.35)

            job = await losing.get(job_id)
            assert job is not None
            assert finished == [], "the handler kept working on a job we had lost"
            assert job.state is JobState.FAILED, "the losing worker ended someone's job"
            assert job.error == "ended from elsewhere", (
                "the losing worker overwrote the outcome"
            )
        finally:
            worker.request_stop()
            await asyncio.wait_for(loop_task, 5.0)

    async def test_a_heartbeat_that_blows_up_does_not_take_the_job_down(
        self,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        # The lease is a safety net, not the work. A database hiccup while
        # extending it must not replace the outcome the handler earned.
        broken = BrokenHeartbeatQueue(session_factory)
        processed: list[str] = []

        async def outlives_a_heartbeat(job: SyncJob, queue_: JobQueue) -> None:
            # Without this the handler could finish first and the broken
            # heartbeat would never have run — the test would prove nothing.
            await asyncio.wait_for(broken.attempted.wait(), 5.0)
            processed.extend(MESSAGES)

        job_id = await broken.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(
            broken, session_factory, {JobKind.IMPORT: outlives_a_heartbeat}
        )
        job = await run_until_done(worker, broken, job_id)

        assert broken.attempted.is_set()
        assert job.state is JobState.SUCCEEDED
        assert processed == list(MESSAGES)


class TestCancelling:
    async def test_cancel_takes_effect_between_two_batches(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        # Phase 2 DoD, third promise. The cancel arrives while batch one is
        # half written; batch one still lands whole, and batch two never runs.
        processed: list[str] = []
        inside_batch = asyncio.Event()
        finish_batch = asyncio.Event()

        async def batched(job: SyncJob, queue_: JobQueue) -> None:
            for offset in range(0, len(MESSAGES), BATCH_SIZE):
                if await queue_.is_cancel_requested(job.id):
                    return
                inside_batch.set()
                await finish_batch.wait()
                processed.extend(MESSAGES[offset : offset + BATCH_SIZE])

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(queue, session_factory, {JobKind.IMPORT: batched})
        loop_task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(inside_batch.wait(), 5.0)
            assert await queue.request_cancel(job_id) is True
            assert processed == []  # the batch is still mid-flight
            finish_batch.set()
            job = await wait_for_state(queue, job_id)
        finally:
            finish_batch.set()
            worker.request_stop()
            await asyncio.wait_for(loop_task, 5.0)

        assert job.state is JobState.CANCELLED
        assert processed == list(MESSAGES[:BATCH_SIZE])

    async def test_a_handler_that_finishes_without_a_cancel_succeeds(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        processed: list[str] = []
        job_id = await queue.enqueue(JobKind.IMPORT, account_id)

        worker = worker_for(
            queue,
            session_factory,
            {JobKind.IMPORT: importing(session_factory, processed)},
        )
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.SUCCEEDED
        assert job.cancel_requested is False


class TestErrorTaxonomy:
    async def test_an_auth_error_ends_the_job_and_marks_the_account(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        async def revoked(job: SyncJob, queue_: JobQueue) -> None:
            raise MailAuthError("the refresh token was revoked")

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(queue, session_factory, {JobKind.IMPORT: revoked})
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.FAILED
        assert job.error is not None
        assert job.error.startswith("auth:")

        async with session_factory() as session:
            account = await session.get(MailAccountEntity, account_id)
            assert account is not None
            assert account.status == AccountStatus.AUTH_ERROR
            assert account.last_error == "the refresh token was revoked"

    async def test_an_auth_error_is_never_retried(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        # No amount of retrying fixes a revoked token; one call is the whole
        # budget.
        calls = 0

        async def revoked(job: SyncJob, queue_: JobQueue) -> None:
            nonlocal calls
            calls += 1
            raise MailAuthError("no")

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(queue, session_factory, {JobKind.IMPORT: revoked})
        await run_until_done(worker, queue, job_id)

        assert calls == 1

    async def test_a_transient_error_is_retried_and_the_job_still_succeeds(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        calls = 0

        async def flaky(job: SyncJob, queue_: JobQueue) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise MailTransientError("503 from upstream")

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        # Explicitly, because the default is one attempt: the import handler
        # retries inside the engine, so a second budget out here would only
        # multiply. A handler that does no retrying of its own asks for this.
        worker = worker_for(
            queue, session_factory, {JobKind.IMPORT: flaky}, max_attempts=3
        )
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.SUCCEEDED
        assert calls == 3

    async def test_by_default_a_transient_error_is_not_retried_here(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        """The engine already retried it five times; twenty-five is not better.

        A transient error that reaches the loop means an outage rather than a
        hiccup, and every extra attempt replays a whole walk of the mailbox.
        """
        calls = 0

        async def rate_limited(job: SyncJob, queue_: JobQueue) -> None:
            nonlocal calls
            calls += 1
            raise MailTransientError("429")

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(queue, session_factory, {JobKind.IMPORT: rate_limited})
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.FAILED
        assert calls == 1

    async def test_a_transient_error_that_never_clears_ends_the_job(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        calls = 0

        async def rate_limited(job: SyncJob, queue_: JobQueue) -> None:
            nonlocal calls
            calls += 1
            raise MailTransientError("429")

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(
            queue, session_factory, {JobKind.IMPORT: rate_limited}, max_attempts=3
        )
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.FAILED
        assert job.error is not None
        assert job.error.startswith("transient:")
        assert calls == 3

    async def test_a_permanent_error_leaves_a_row_in_mail_failed_messages(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        # §7.6 forbids a silent drop. The worker no longer knows which message
        # it was, but the row is written all the same.
        async def broken(job: SyncJob, queue_: JobQueue) -> None:
            raise MailPermanentError("the MIME boundary never appears")

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(queue, session_factory, {JobKind.IMPORT: broken})
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.FAILED
        async with session_factory() as session:
            rows = (
                (await session.execute(select(MailFailedMessageEntity))).scalars().all()
            )
        assert len(rows) == 1
        assert rows[0].account_id == account_id
        assert rows[0].reason == "permanent"
        assert rows[0].detail == "the MIME boundary never appears"

    async def test_a_job_without_an_account_has_no_ledger_to_write_to(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        # `derive` and `embed` name no mailbox, so there is no account to mark
        # and no message to blame. The job still ends with its reason.
        async def broken(job: SyncJob, queue_: JobQueue) -> None:
            raise MailPermanentError("nothing to blame this on")

        job_id = await queue.enqueue(JobKind.DERIVE)
        worker = worker_for(queue, session_factory, {JobKind.DERIVE: broken})
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.FAILED
        assert job.error is not None
        assert job.error.startswith("permanent:")
        async with session_factory() as session:
            rows = (
                (await session.execute(select(MailFailedMessageEntity))).scalars().all()
            )
        assert rows == []

    async def test_an_auth_error_without_an_account_marks_nothing(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        async def revoked(job: SyncJob, queue_: JobQueue) -> None:
            raise MailAuthError("no credentials at all")

        job_id = await queue.enqueue(JobKind.EMBED)
        worker = worker_for(queue, session_factory, {JobKind.EMBED: revoked})
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.FAILED
        async with session_factory() as session:
            account = await session.get(MailAccountEntity, account_id)
            assert account is not None
            assert account.status == AccountStatus.IDLE

    async def test_an_unexpected_exception_fails_the_job_and_names_its_type(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        async def buggy(job: SyncJob, queue_: JobQueue) -> None:
            raise ValueError("off by one")

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(queue, session_factory, {JobKind.IMPORT: buggy})
        job = await run_until_done(worker, queue, job_id)

        assert job.state is JobState.FAILED
        assert job.error == "ValueError: off by one"

    async def test_the_worker_keeps_going_after_a_job_fails(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        processed: list[str] = []

        async def buggy(job: SyncJob, queue_: JobQueue) -> None:
            raise ValueError("off by one")

        first = await queue.enqueue(JobKind.IMPORT, account_id)
        second = await queue.enqueue(JobKind.INCREMENTAL, account_id)
        worker = worker_for(
            queue,
            session_factory,
            {
                JobKind.IMPORT: buggy,
                JobKind.INCREMENTAL: importing(session_factory, processed),
            },
        )
        loop_task = asyncio.create_task(worker.run())
        try:
            failed = await wait_for_state(queue, first)
            done = await wait_for_state(queue, second)
        finally:
            worker.request_stop()
            await asyncio.wait_for(loop_task, 5.0)

        assert failed.state is JobState.FAILED
        assert done.state is JobState.SUCCEEDED
        assert processed == list(MESSAGES)


class TestBackoff:
    def test_the_wait_grows_and_stays_under_the_ceiling(self, queue: JobQueue) -> None:
        worker = JobWorker(
            queue,
            {},
            backoff_seconds=1.0,
            max_backoff_seconds=8.0,
            handle_signals=False,
        )

        assert 1.0 <= worker._backoff(0, None) <= 1.25
        assert 2.0 <= worker._backoff(1, None) <= 2.5
        assert 8.0 <= worker._backoff(9, None) <= 10.0

    def test_retry_after_is_a_floor_the_backoff_never_undercuts(
        self, queue: JobQueue
    ) -> None:
        worker = JobWorker(
            queue,
            {},
            backoff_seconds=1.0,
            max_backoff_seconds=8.0,
            handle_signals=False,
        )

        assert worker._backoff(0, 30.0) >= 30.0

    def test_two_waits_of_the_same_length_differ(self, queue: JobQueue) -> None:
        # The jitter is the point: a hundred jobs that failed together must not
        # come back together.
        worker = JobWorker(queue, {}, backoff_seconds=10.0, handle_signals=False)

        waits = {worker._backoff(0, None) for _ in range(20)}

        assert len(waits) > 1


class TestSignals:
    async def test_sigterm_stops_the_loop(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        saved = signal.getsignal(signal.SIGTERM)
        worker = worker_for(queue, session_factory, {}, handle_signals=True)
        loop_task = asyncio.create_task(worker.run())
        try:
            await asyncio.sleep(0.05)
            if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
                pytest.skip("no asyncio signal handlers on this platform")
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(loop_task, 5.0)
        finally:
            worker.request_stop()
            if not loop_task.done():
                await asyncio.wait_for(loop_task, 5.0)
            signal.signal(signal.SIGTERM, saved)

    async def test_a_cancelled_run_leaves_no_task_behind(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
    ) -> None:
        # The job keeps its lease; the next start reclaims it. That is the same
        # path a kill -9 takes, deliberately.
        started = asyncio.Event()

        async def slow(job: SyncJob, queue_: JobQueue) -> None:
            started.set()
            await asyncio.sleep(30)

        job_id = await queue.enqueue(JobKind.IMPORT, account_id)
        worker = worker_for(queue, session_factory, {JobKind.IMPORT: slow})
        loop_task = asyncio.create_task(worker.run())
        await asyncio.wait_for(started.wait(), 5.0)

        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task

        job = await queue.get(job_id)
        assert job is not None
        assert job.state is JobState.RUNNING
        assert len(pending_tasks()) == 0


class CountingQueue(JobQueue):
    """A real queue that also says how often it was asked for a heartbeat."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self.heartbeats = 0
        self.beaten = asyncio.Event()

    async def heartbeat(self, job_id: int, lease_seconds: float) -> bool:
        self.heartbeats += 1
        self.beaten.set()
        return await super().heartbeat(job_id, lease_seconds)


class LosingQueue(CountingQueue):
    """A real queue that also says *when* it refused to extend a lease."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self.refused = asyncio.Event()

    async def heartbeat(self, job_id: int, lease_seconds: float) -> bool:
        held = await super().heartbeat(job_id, lease_seconds)
        if not held:
            self.refused.set()
        return held


class BrokenHeartbeatQueue(JobQueue):
    """A queue whose heartbeat raises rather than answering."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self.attempted = asyncio.Event()

    async def heartbeat(self, job_id: int, lease_seconds: float) -> bool:
        self.attempted.set()
        raise RuntimeError("the database went away")


async def never_called(job: SyncJob, queue: JobQueue) -> None:
    raise AssertionError("this handler must not run")


async def wait_for_heartbeats(
    queue: CountingQueue, at_least: int, timeout: float = 5.0
) -> None:
    """Poll until the worker has pushed the lease out ``at_least`` times."""

    async def poll() -> None:
        while queue.heartbeats < at_least:
            queue.beaten.clear()
            await queue.beaten.wait()

    await asyncio.wait_for(poll(), timeout)


async def wait_for_lease_beyond(
    queue: JobQueue, job_id: int, lease_until: datetime, timeout: float = 5.0
) -> SyncJob:
    """Poll until the heartbeat has moved the lease past ``lease_until``."""

    async def poll() -> SyncJob:
        while True:
            job = await queue.get(job_id)
            if job is not None and job.lease_until and job.lease_until > lease_until:
                return job
            await asyncio.sleep(0.005)

    return await asyncio.wait_for(poll(), timeout)


def pending_tasks() -> list[asyncio.Task]:
    """Tasks other than this one that are still alive."""
    current = asyncio.current_task()
    return [
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    ]
