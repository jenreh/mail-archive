"""Tests for :class:`mailarc_sync.jobs.sweep.MailboxSweep`.

The scheduled half of this class is already pinned through
``test_jobs_scheduler``, which drives it by its old name — ``tick()`` — over
the same database and has not been rewritten, on purpose: a refactor whose
tests move with it proves nothing about what the refactor preserved. What is
tested here is the half that is new, and the one assertion that ties the two
together.

That assertion is :class:`TestTheDifferenceBetweenTheTwoSweeps`. A mailbox
nobody has imported yet is the entire reason the button is not simply
``tick()``: the schedule leaves it alone because a delta has nowhere to start,
and a Sync button that inherited that silence would do visibly nothing on the
one archive where a person is most likely to press it — the fresh install with
an account added five minutes ago.

Against a real SQLite file for :mod:`test_jobs_scheduler`'s reason: what a
sweep decides is decided out of two tables, and a double for either would be
free to answer them the way this module hopes. Nothing here opens a mailbox.
"""

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
from mailarc_sync.engine.engine import INCREMENTAL_SCOPE
from mailarc_sync.engine.registry import ProviderRegistry
from mailarc_sync.jobs.model import JobKind, SyncJob
from mailarc_sync.jobs.queue import JobQueue, SessionFactory
from mailarc_sync.jobs.sweep import MAILBOX_KINDS, MailboxSweep, SweepReport


def a_registry(*, deltas: bool = True) -> ProviderRegistry:
    """A registry holding the fake provider, with or without a delta.

    The factory is never called. A sweep that built a source would be opening
    somebody's mailbox in order to decide whether to queue a job that opens
    somebody's mailbox.
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
    raise AssertionError("the sweep opened a mailbox")


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
    """One enabled account of the fake provider, imported once already."""
    return await stored_account(session_factory)


async def stored_account(
    session_factory: SessionFactory,
    *,
    address: str = "jens@example.com",
    provider: str = MailProvider.FAKE,
    enabled: bool = True,
    status: str = AccountStatus.IDLE,
    armed: bool = True,
) -> int:
    """An account, by default one whose first full import has finished."""
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
        async with session_factory() as session:
            await SyncCheckpointRepository().upsert_cursor(
                session, account_id, INCREMENTAL_SCOPE, "1000", 0
            )
    return account_id


async def end_job(session_factory: SessionFactory, job_id: int) -> None:
    """Take a job out of the open states without running it."""
    async with session_factory() as session:
        job = await session.get(MailSyncJobEntity, job_id)
        assert job is not None
        job.state = SyncJobState.SUCCEEDED


def a_sweep(
    queue: JobQueue,
    session_factory: SessionFactory,
    *,
    registry: ProviderRegistry | None = None,
) -> MailboxSweep:
    return MailboxSweep(
        queue, registry or a_registry(), session_factory=session_factory
    )


async def kinds_of(queue: JobQueue, report: SweepReport) -> list[JobKind]:
    """What the sweep actually queued, read back off the rows it wrote."""
    kinds = []
    for job_id in report.queued:
        job = await queue.get(job_id)
        assert job is not None
        kinds.append(job.kind)
    return kinds


class TestWhatTheButtonQueues:
    async def test_an_armed_mailbox_gets_a_delta(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """A mailbox that has been imported does not get imported again: the
        cheap question is the right one, and the expensive one is the same
        answer with twenty years of listing in front of it."""
        report = await a_sweep(queue, session_factory).everything()

        assert await kinds_of(queue, report) == [JobKind.INCREMENTAL]
        job = await queue.get(report.queued[0])
        assert job is not None
        assert job.account_id == account_id

    async def test_a_mailbox_nobody_has_imported_gets_a_full_import(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """The button's whole reason for existing beside the schedule."""
        await stored_account(session_factory, armed=False)

        report = await a_sweep(queue, session_factory).everything()

        assert await kinds_of(queue, report) == [JobKind.IMPORT]

    async def test_a_provider_without_a_delta_gets_a_full_import(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """A provider that cannot say what changed can still say what is there,
        and the engine archives only what is not archived yet — so the full
        walk is the honest answer rather than a fallback."""
        sweep = a_sweep(queue, session_factory, registry=a_registry(deltas=False))

        report = await sweep.everything()

        assert await kinds_of(queue, report) == [JobKind.IMPORT]

    async def test_two_mailboxes_are_two_jobs(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        await stored_account(session_factory, address="second@example.com")

        report = await a_sweep(queue, session_factory).everything()

        assert len(report.queued) == 2
        assert report.considered == 2

    async def test_a_disabled_mailbox_is_not_even_considered(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """``enabled`` is the archive's own word for "sync this one"; a sweep
        that reported it as skipped would invite a person to go and fix it."""
        await stored_account(session_factory, enabled=False)

        report = await a_sweep(queue, session_factory).everything()

        assert report == SweepReport()
        assert report.considered == 0

    async def test_an_archive_with_no_mailboxes_queues_nothing(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """Which is the state a fresh install presses the button in, and it has
        to be distinguishable from "everything is already running"."""
        report = await a_sweep(queue, session_factory).everything()

        assert report.considered == 0


class TestWhatTheButtonRefuses:
    async def test_a_mailbox_already_syncing_is_reported_busy(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """Two runs over one mailbox is not a wasted job, it is an
        ``IntegrityError``: both write ``mail_archived_messages`` under a
        unique key on (account, message)."""
        first = await a_sweep(queue, session_factory).everything()

        second = await a_sweep(queue, session_factory).everything()

        assert len(first.queued) == 1
        assert second.queued == ()
        assert second.busy == (account_id,)

    @pytest.mark.parametrize("kind", MAILBOX_KINDS)
    async def test_either_kind_of_open_job_makes_a_mailbox_busy(
        self,
        queue: JobQueue,
        session_factory: SessionFactory,
        account_id: int,
        kind: JobKind,
    ) -> None:
        """The import is the one that is easy to miss: a human pressing Import
        on the accounts page must not have their run broken by somebody
        pressing Sync in the rail a second later."""
        await queue.enqueue(kind, account_id)

        report = await a_sweep(queue, session_factory).everything()

        assert report.busy == (account_id,)

    async def test_a_job_that_has_ended_does_not_hold_a_mailbox_back(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        job_id = await queue.enqueue(JobKind.INCREMENTAL, account_id)
        await end_job(session_factory, job_id)

        report = await a_sweep(queue, session_factory).everything()

        assert len(report.queued) == 1

    async def test_a_mailbox_waiting_for_a_re_consent_is_blocked(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """Nothing but a human moves an account out of ``auth_error``, so a
        job queued for one is a failure row with a provider hammered to earn
        it. Blocked rather than skipped, because a person who just pressed
        Sync is exactly the person who can fix it."""
        blocked = await stored_account(session_factory, status=AccountStatus.AUTH_ERROR)

        report = await a_sweep(queue, session_factory).everything()

        assert report.queued == ()
        assert report.blocked == (blocked,)

    async def test_a_provider_this_process_cannot_open_is_blocked_not_imported(
        self, queue: JobQueue, session_factory: SessionFactory, caplog
    ) -> None:
        """The trap in "a mailbox without a delta gets a full import": an
        account naming a provider this build never registered has no delta
        either, and falling through to the import would queue a job that fails
        the moment a worker claims it. A build without the Gmail component
        still has the Gmail rows."""
        unopenable = await stored_account(session_factory, provider=MailProvider.GMAIL)

        report = await a_sweep(queue, session_factory).everything()

        assert report.queued == ()
        assert report.blocked == (unopenable,)
        assert "cannot open" in caplog.text

    async def test_a_provider_the_domain_has_never_heard_of_is_blocked(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """``mail_accounts.provider`` is a plain string on purpose, so a value
        that is not a ``MailProvider`` at all is reachable."""
        pigeon = await stored_account(session_factory, provider="carrier-pigeon")

        report = await a_sweep(queue, session_factory).everything()

        assert report.blocked == (pigeon,)


class TestTheDifferenceBetweenTheTwoSweeps:
    """One mailbox, two sweeps, two answers — and both are right."""

    async def test_the_schedule_leaves_an_unimported_mailbox_alone(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """A delta over a mailbox nobody has walked bootstraps at today's
        watermark and reports success having archived nothing, and every sweep
        after it is a delta on that. The timer must not do this on its own."""
        await stored_account(session_factory, armed=False)

        assert await a_sweep(queue, session_factory).deltas() == []

    async def test_the_button_imports_the_same_mailbox(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """Because somebody asked for it, which is the difference the split
        exists to hold."""
        await stored_account(session_factory, armed=False)

        report = await a_sweep(queue, session_factory).everything()

        assert await kinds_of(queue, report) == [JobKind.IMPORT]

    async def test_they_agree_about_a_mailbox_waiting_for_a_re_consent(
        self, queue: JobQueue, session_factory: SessionFactory
    ) -> None:
        """The half of the policy that must never differ: a hammered provider
        is a hammered provider whoever asked."""
        await stored_account(session_factory, status=AccountStatus.AUTH_ERROR)

        assert await a_sweep(queue, session_factory).deltas() == []
        assert (await a_sweep(queue, session_factory).everything()).queued == ()


class TestOneMailboxThatGoesWrong:
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

    async def test_the_other_mailboxes_are_still_queued(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        second = await stored_account(session_factory, address="second@example.com")
        sweep = a_sweep(self.OneRefusal(queue, refuses=account_id), session_factory)

        report = await sweep.everything()

        assert len(report.queued) == 1
        job = await queue.get(report.queued[0])
        assert job is not None
        assert job.account_id == second

    async def test_the_failure_is_written_down(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int, caplog
    ) -> None:
        """§7.6's rule holds here too: nothing is dropped without a trace."""
        sweep = a_sweep(self.OneRefusal(queue, refuses=account_id), session_factory)

        await sweep.everything()

        assert "Could not schedule account" in caplog.text
        assert "database is locked" in caplog.text

    async def test_a_mailbox_that_went_wrong_is_in_no_bucket_at_all(
        self, queue: JobQueue, session_factory: SessionFactory, account_id: int
    ) -> None:
        """It is not busy, it is not blocked and it was not queued — the log
        is where a failure lives, and a report that guessed would tell a person
        to go and fix the wrong thing."""
        sweep = a_sweep(self.OneRefusal(queue, refuses=account_id), session_factory)

        report = await sweep.everything()

        assert account_id not in report.busy
        assert account_id not in report.blocked
        assert report.considered == 0
