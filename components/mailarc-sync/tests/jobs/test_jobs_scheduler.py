"""Tests for :class:`mailarc_sync.jobs.scheduler.IntervalScheduler`.

Against a real SQLite file like the queue's own tests, because what a sweep
decides is decided out of two tables: which accounts are enabled, and which of
them already have a job going. A double for either would be free to answer
those the way this module hopes.

The loop's tests do not sleep out an interval. They count sweeps through a
subclass and wait on an :class:`asyncio.Event`, so "it keeps sweeping" is
settled by the second sweep arriving rather than by a timer that is fast enough
on this machine.

Nothing here opens a mailbox: a scheduler that reached a provider would be the
bug the descriptor exists to prevent.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from appkit_commons.database.entities import Base
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
from mailarc_sync.engine.engine import FULL_SCOPE, INCREMENTAL_SCOPE
from mailarc_sync.engine.registry import ProviderRegistry
from mailarc_sync.jobs.model import JobKind, SyncJob
from mailarc_sync.jobs.queue import JobQueue, SessionFactory
from mailarc_sync.jobs.scheduler import IntervalScheduler

FAST = 0.01
"""An interval short enough that a loop test finishes, long enough to be a wait."""

SLOW = 30.0
"""An interval no test may wait out — used to prove that nothing swept yet."""

PATIENCE = 5.0
"""How long a loop test waits for an event before calling the loop broken."""


def a_registry(*, deltas: bool = True) -> ProviderRegistry:
    """A registry holding the fake provider, with or without a delta.

    The factory is never called. A scheduler that built a source would be
    opening somebody's mailbox in order to decide whether to queue a job that
    opens somebody's mailbox.
    """
    registry = ProviderRegistry()
    registry.register(
        ProviderDescriptor(
            provider=MailProvider.FAKE,
            label="Folder of .eml files",
            supports_incremental=deltas,
        ),
        _refuse_to_open,
    )
    return registry


def _refuse_to_open(account: MailAccountEntity, secret: str) -> MailSourcePort:
    raise AssertionError("the scheduler opened a mailbox")


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
    """One enabled account of the fake provider — the mailbox a sweep is for."""
    return await stored_account(session_factory)


async def checkpointed(
    session_factory: SessionFactory, account_id: int, scope: str, cursor: str
) -> None:
    """Leave behind what a run of that scope would have left behind."""
    async with session_factory() as session:
        await SyncCheckpointRepository().upsert_cursor(
            session, account_id, scope, cursor, 0
        )


async def stored_account(
    session_factory: SessionFactory,
    *,
    address: str = "jens@example.com",
    provider: str = MailProvider.FAKE,
    enabled: bool = True,
    status: str = AccountStatus.IDLE,
    armed: bool = True,
) -> int:
    """An account, by default one whose first full import has finished.

    ``armed`` is that finished import, and it is the default because it is the
    ordinary state of every mailbox the schedule is for: a human pressed Import
    once, it ran to the end, and it left the incremental starting point behind.
    An account without one is the exception, and the tests that want it say so.
    """
    async with session_factory() as session:
        account = MailAccountEntity(
            provider=provider,
            display_name="Work",
            email_address=address,
            enabled=enabled,
            status=status,
        )
        session.add(account)
        await session.flush()
        account_id = account.id
    if armed:
        await checkpointed(session_factory, account_id, INCREMENTAL_SCOPE, "1000")
    return account_id


async def end_job(session_factory: SessionFactory, job_id: int) -> None:
    """Take a job out of the open states without running it."""
    async with session_factory() as session:
        job = await session.get(MailSyncJobEntity, job_id)
        assert job is not None
        job.state = SyncJobState.SUCCEEDED


def a_scheduler(
    queue: JobQueue,
    session_factory: SessionFactory,
    *,
    registry: ProviderRegistry | None = None,
    interval_seconds: float = SLOW,
) -> IntervalScheduler:
    return IntervalScheduler(
        queue,
        registry or a_registry(),
        interval_seconds=interval_seconds,
        session_factory=session_factory,
    )


class CountingSchedule(IntervalScheduler):
    """A schedule whose sweep is counted instead of performed.

    The loop and the sweep are separate concerns and are tested separately:
    what a sweep decides is settled against the database above, and what the
    loop does with a sweep — waits first, keeps going, stops when asked — has
    nothing to do with mailboxes.
    """

    def __init__(
        self,
        *,
        sweeps: int = 1,
        explodes: int = 0,
        stops_itself: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.ticks = 0
        self.enough = asyncio.Event()
        self._sweeps = sweeps
        self._explodes = explodes
        self._stops_itself = stops_itself

    async def tick(self) -> list[int]:
        self.ticks += 1
        if self.ticks >= self._sweeps:
            self.enough.set()
        if self._stops_itself:
            self.request_stop()
        if self.ticks <= self._explodes:
            raise RuntimeError("the database went away mid-sweep")
        return [self.ticks]


def a_counting_schedule(
    queue: JobQueue,
    session_factory: SessionFactory,
    *,
    interval_seconds: float = FAST,
    sweeps: int = 1,
    explodes: int = 0,
    stops_itself: bool = False,
) -> CountingSchedule:
    return CountingSchedule(
        queue=queue,
        registry=a_registry(),
        interval_seconds=interval_seconds,
        session_factory=session_factory,
        sweeps=sweeps,
        explodes=explodes,
        stops_itself=stops_itself,
    )


class TestWhatOneSweepQueues:
    async def test_an_enabled_account_gets_an_incremental_job(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        queued = await a_scheduler(queue, session_factory).tick()

        assert len(queued) == 1
        job = await queue.get(queued[0])
        assert job is not None
        assert job.kind is JobKind.INCREMENTAL
        assert job.account_id == account_id

    async def test_a_disabled_account_is_left_alone(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """``enabled`` is the human's switch, and a schedule is not a human."""
        await stored_account(session_factory, enabled=False)

        assert await a_scheduler(queue, session_factory).tick() == []

    async def test_an_account_that_is_already_syncing_is_skipped(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """Otherwise a mailbox slower than the interval collects a queue of
        deltas, each of which will walk the same history again."""
        await queue.enqueue(JobKind.INCREMENTAL, account_id)

        assert await a_scheduler(queue, session_factory).tick() == []

    async def test_an_account_with_an_open_full_import_is_skipped(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """The one that is easy to miss: both kinds write the same full-scope
        checkpoint and both insert into ``mail_archived_messages``, whose
        unique key turns the second writer's batch into an ``IntegrityError``.
        A sweep must not break the import a human just started."""
        await queue.enqueue(JobKind.IMPORT, account_id)

        assert await a_scheduler(queue, session_factory).tick() == []

    async def test_a_job_that_has_ended_does_not_block_the_next_sweep(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """ "Open" is the point, not "exists" — otherwise the first sync a
        mailbox ever ran would be its last."""
        finished = await queue.enqueue(JobKind.IMPORT, account_id)
        await end_job(session_factory, finished)

        assert len(await a_scheduler(queue, session_factory).tick()) == 1

    async def test_a_job_for_another_account_does_not_block_this_one(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        other = await stored_account(session_factory, address="second@example.com")
        await queue.enqueue(JobKind.INCREMENTAL, other)

        queued = await a_scheduler(queue, session_factory).tick()

        assert len(queued) == 1
        job = await queue.get(queued[0])
        assert job is not None
        assert job.account_id == account_id

    async def test_an_account_waiting_for_a_re_consent_is_skipped(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """Nothing but a human moves an account out of ``auth_error``, so a
        sweep that queued one would fail a job, hammer the provider and fill
        the table with identical failures — every interval, for ever."""
        await stored_account(session_factory, status=AccountStatus.AUTH_ERROR)

        assert await a_scheduler(queue, session_factory).tick() == []

    async def test_an_account_that_has_never_finished_a_full_import_is_skipped(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """A delta over a mailbox nobody has walked archives nothing, for ever.

        The engine has no history to ask about, so it bootstraps at today's
        watermark and returns having done nothing — successfully. Every sweep
        after that fetches only mail newer than the sweep that armed it, and
        the twenty years already in the mailbox are in no run at all. Nothing
        would say so: the job row reads succeeded, the account shows no error.
        The first sync stays a button a human presses.
        """
        await stored_account(session_factory, address="never@example.com", armed=False)

        assert await a_scheduler(queue, session_factory).tick() == []

    async def test_an_import_that_was_cancelled_halfway_does_not_arm_the_schedule(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """It left a page token, not a starting point — the walk is unfinished."""
        unfinished = await stored_account(
            session_factory, address="half@example.com", armed=False
        )
        await checkpointed(session_factory, unfinished, FULL_SCOPE, "page-400")

        assert await a_scheduler(queue, session_factory).tick() == []

    async def test_a_provider_that_cannot_do_deltas_is_skipped(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """``supports_incremental`` gets its first consumer outside a test:
        scheduling a delta against a provider that has none would queue a job
        whose every run walks the whole mailbox."""
        scheduler = a_scheduler(
            queue, session_factory, registry=a_registry(deltas=False)
        )

        assert await scheduler.tick() == []

    async def test_a_provider_this_process_never_registered_is_skipped(
        self, queue: JobQueue, session_factory: SessionFactory, caplog
    ) -> None:
        """A build without the Gmail component still has the Gmail rows."""
        await stored_account(session_factory, provider=MailProvider.GMAIL)

        assert await a_scheduler(queue, session_factory).tick() == []
        assert "cannot open" in caplog.text

    async def test_a_provider_the_domain_has_never_heard_of_is_skipped(
        self, queue: JobQueue, session_factory: SessionFactory, caplog
    ) -> None:
        """``mail_accounts.provider`` is a plain string on purpose, so a value
        that is not a ``MailProvider`` at all is reachable — a downgrade, or a
        row somebody wrote by hand."""
        await stored_account(session_factory, provider="carrier-pigeon")

        assert await a_scheduler(queue, session_factory).tick() == []
        assert "cannot open" in caplog.text

    async def test_two_mailboxes_are_two_jobs(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        await stored_account(session_factory, address="second@example.com")

        assert len(await a_scheduler(queue, session_factory).tick()) == 2


class TestOneAccountThatGoesWrong:
    """A sweep is several mailboxes, and one of them belongs to somebody else."""

    class OneRefusal(JobQueue):
        """A queue that cannot answer for one account and is fine for the rest."""

        def __init__(self, inner: JobQueue, refuses: int) -> None:
            self._inner = inner
            self._refuses = refuses

        async def find_open(
            self, kind: JobKind, account_id: int | None = None
        ) -> SyncJob | None:
            if account_id == self._refuses:
                raise RuntimeError("database is locked")
            return await self._inner.find_open(kind, account_id)

        async def enqueue(self, kind: JobKind, account_id: int | None = None) -> int:
            return await self._inner.enqueue(kind, account_id)

    async def test_the_other_accounts_are_still_queued(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        second = await stored_account(session_factory, address="second@example.com")
        scheduler = a_scheduler(
            self.OneRefusal(queue, refuses=account_id), session_factory
        )

        queued = await scheduler.tick()

        assert len(queued) == 1
        job = await queue.get(queued[0])
        assert job is not None
        assert job.account_id == second

    async def test_the_failure_is_written_down(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int, caplog
    ) -> None:
        """§7.6's rule holds here too: nothing is dropped without a trace."""
        scheduler = a_scheduler(
            self.OneRefusal(queue, refuses=account_id), session_factory
        )

        await scheduler.tick()

        assert "Could not schedule account" in caplog.text
        assert "database is locked" in caplog.text


class TestTheLoop:
    """When a sweep happens, and that nothing can make it the last one."""

    @staticmethod
    async def _running(schedule: CountingSchedule) -> asyncio.Task[None]:
        task = asyncio.create_task(schedule.run())
        await asyncio.sleep(0)
        return task

    @staticmethod
    async def _stop(schedule: CountingSchedule, task: asyncio.Task[None]) -> None:
        schedule.request_stop()
        await asyncio.wait_for(task, timeout=PATIENCE)

    async def test_an_interval_of_zero_never_sweeps_at_all(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """The default, and the reason it is the default: a fresh install must
        not start talking to somebody's mailbox on its own."""
        schedule = a_counting_schedule(queue, session_factory, interval_seconds=0.0)

        await asyncio.wait_for(schedule.run(), timeout=PATIENCE)

        assert schedule.ticks == 0

    async def test_it_waits_before_the_first_sweep(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """A desktop application restarts every time a lid closes; sweeping on
        startup would turn each of those into a round of syncs."""
        schedule = a_counting_schedule(
            queue, session_factory, interval_seconds=SLOW, sweeps=1
        )
        task = await self._running(schedule)

        await asyncio.sleep(0)

        assert schedule.ticks == 0
        await self._stop(schedule, task)

    async def test_it_keeps_sweeping_until_it_is_asked_to_stop(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        schedule = a_counting_schedule(queue, session_factory, sweeps=2)
        task = await self._running(schedule)

        await asyncio.wait_for(schedule.enough.wait(), timeout=PATIENCE)
        await self._stop(schedule, task)

        assert schedule.ticks >= 2
        assert task.done()

    async def test_a_sweep_that_throws_does_not_end_the_schedule(
        self, queue: JobQueue, session_factory: SessionFactory, caplog
    ) -> None:
        """The database is what goes away here — a locked file, a directory
        gone under a sleeping laptop — and none of that is a reason to stop
        looking for mail for the rest of the process's life."""
        schedule = a_counting_schedule(queue, session_factory, sweeps=2, explodes=1)
        task = await self._running(schedule)

        await asyncio.wait_for(schedule.enough.wait(), timeout=PATIENCE)
        await self._stop(schedule, task)

        assert schedule.ticks >= 2
        assert "A sweep for new mail failed" in caplog.text

    async def test_a_stop_asked_for_before_the_first_sweep_is_honoured(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """The flag is what the interval waits on, so a schedule parked on
        fifteen minutes stops in the time it takes to run one ``if``."""
        schedule = a_counting_schedule(
            queue, session_factory, interval_seconds=SLOW, sweeps=1
        )
        task = await self._running(schedule)

        await self._stop(schedule, task)

        assert schedule.ticks == 0

    async def test_a_stop_that_arrives_mid_sweep_ends_the_loop_after_it(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """The sweep it interrupted still finishes — it is only ever an
        enqueue, and half a sweep is a mailbox skipped for one interval. What
        must not happen is a second one afterwards.

        The stop is then asked for a second time, which is what the worker's
        ``finally`` does after a signal handler has already asked: saying it
        twice has to be a no-op, not a second flag to clear."""
        schedule = a_counting_schedule(queue, session_factory, stops_itself=True)
        task = await self._running(schedule)

        await asyncio.wait_for(schedule.enough.wait(), timeout=PATIENCE)
        await self._stop(schedule, task)

        assert schedule.ticks == 1

    async def test_a_cancelled_loop_stops(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """What the worker falls back on when a stop request is not enough."""
        schedule = a_counting_schedule(
            queue, session_factory, interval_seconds=SLOW, sweeps=1
        )
        task = await self._running(schedule)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert schedule.ticks == 0
