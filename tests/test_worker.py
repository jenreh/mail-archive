"""What the worker process makes of a job, and what it refuses to carry.

The poll loop belongs to ``mailarc_sync`` and is tested there. What is only
true here is the wiring: which kind of job runs which handler, that a handler
turns the account row a job names into an open mailbox — credential included —
or says clearly why it cannot, and what a job queues once it is done. The two
kinds that name no mailbox — ``derive`` and ``embed`` — are next door in
``test_worker_analysis.py``, and the doubles both modules stand things in with
are in ``worker_doubles.py``.

``test_the_worker_process_carries_no_ui_framework`` is a subprocess, for the
reason ``test_isolation.py`` gives: this suite has Reflex in its interpreter
already, so an in-process check that the worker stays free of it would prove
nothing.
"""

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from typing import cast

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import composition, worker
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.database.entities import (
    CredentialKind,
    MailAccountEntity,
    MailArchivedMessageEntity,
    MailCredentialEntity,
    MailSyncJobEntity,
)
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.model import MailProvider, ProviderDescriptor, SyncCursorKind
from mailarc_core.mail.ports import MailSourceFactory
from mailarc_sync.engine import (
    FAKE_DESCRIPTOR,
    FakeMailSource,
    ImportEngine,
    ProviderRegistry,
)
from mailarc_sync.engine.config import SyncConfig
from mailarc_sync.engine.engine import GraphSessionFactory
from mailarc_sync.jobs import JobHandler, JobKind, JobQueue, SessionFactory
from tests.worker_doubles import (
    ADDRESS,
    MAILBOX,
    CancellingQueue,
    FakeSource,
    GraphNotes,
    RecordingEngine,
    RecordingQueue,
    RefusingQueue,
    a_delta_job,
    a_job,
    one_graph_session,
)

FORBIDDEN = ("reflex", "appkit_mantine", "appkit_ui")
"""The worker renders nothing; carrying a UI framework would only cost memory."""

PROBE = """
import importlib, sys

importlib.import_module("app.worker")
print(",".join(sorted(sys.modules)))
"""


@pytest.fixture
async def database(tmp_path) -> AsyncIterator[AsyncEngine]:
    """A fresh database file with the mail tables on it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail-archive.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(database: AsyncEngine) -> SessionFactory:
    """Appkit's transaction semantics: commit on a clean exit, else roll back."""
    return async_sessionmaker(database, expire_on_commit=False).begin


@pytest.fixture
def encryption_key() -> Iterator[str]:
    """`EncryptedString` reads the key off the registry at write time."""
    key = Fernet.generate_key().decode()
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(
        DatabaseConfig, DatabaseConfig.model_validate({"encryption_key": key})
    )
    yield key
    registry.restore(saved)


@pytest.fixture
def registry() -> ProviderRegistry:
    """A registry holding one provider that builds a :class:`FakeSource`."""
    built = ProviderRegistry()
    built.register(
        ProviderDescriptor(provider=MailProvider.FAKE, label="Folder of .eml files"),
        cast(MailSourceFactory, FakeSource),
    )
    return built


async def stored_account(
    session: AsyncSession, with_credential: bool = True, secret: str = MAILBOX
) -> int:
    """An enabled account of the fake provider, with its secret."""
    account = MailAccountEntity(
        provider=MailProvider.FAKE,
        display_name="Exported mailbox",
        email_address=ADDRESS,
    )
    session.add(account)
    await session.flush()
    if with_credential:
        session.add(
            MailCredentialEntity(
                account_id=account.id, kind=CredentialKind.PASSWORD, secret=secret
            )
        )
        await session.flush()
    return account.id


def a_message(number: int) -> bytes:
    """One well-formed message; `number` is all that tells them apart."""
    return (
        f"Message-ID: <m{number}@example.com>\n"
        f"Date: Wed, 04 Mar 2026 09:{number:02d}:00 +0100\n"
        "From: Anna <anna@example.com>\n"
        f"To: {ADDRESS}\n"
        f"Subject: Angebot {number}\n"
        "\n"
        "Hallo, anbei das Angebot.\n"
    ).encode()


def test_the_engine_is_built_from_the_registered_configuration() -> None:
    """Nothing is opened here — a store is a path until something writes."""
    engine = worker.build_engine()

    assert isinstance(engine, ImportEngine)
    assert engine._blobs.root == composition.archive_config().store_dir


def test_every_kind_that_can_be_carried_out_has_a_handler() -> None:
    """Asserted against ``JobKind`` itself rather than against a written-out
    set, because the written-out set is what let ``embed`` ship unmapped for a
    whole phase: the suite pinned the gap instead of exposing it, and a user
    who followed the "then run the embed job" sentence got "no handler
    registered". Since phase 7 that is every kind there is, and the next one
    to arrive fails here instead of in front of somebody."""
    handlers = worker.build_handlers(RecordingEngine(), ProviderRegistry())

    assert set(handlers) == set(JobKind)


async def test_the_loop_takes_its_timings_from_the_configuration(monkeypatch) -> None:
    """``JobWorker`` carries defaults of its own; a lease that disagrees with
    ``app_sync_lease_seconds`` would only show up as a job stolen mid-import."""
    seen: dict[str, object] = {}

    class RecordingWorker:
        def __init__(self, queue, handlers, **timings) -> None:
            seen.update(timings)
            seen["kinds"] = set(handlers)

        async def run(self) -> None:
            seen["ran"] = True

    monkeypatch.setattr(worker, "JobWorker", RecordingWorker)

    await worker.run_worker()

    config = composition.sync_config()
    assert seen["ran"] is True
    assert seen["kinds"] == set(JobKind)
    assert seen["worker_id"] == config.worker_id
    assert seen["lease_seconds"] == config.lease_seconds
    assert seen["heartbeat_seconds"] == config.heartbeat_interval
    assert seen["poll_seconds"] == config.poll_interval


class TestTheSchedule:
    """The recurring trigger, as far as this process is concerned.

    What a sweep decides belongs to ``mailarc_sync`` and is tested there
    against a real database. What is only true here is the lifecycle: the
    schedule is built from the configured interval, it starts beside the poll
    loop, and it is put down whatever ends that loop — including an ending
    nobody planned.
    """

    class RecordingSchedule:
        """A schedule that runs until it is asked to stop, and says it was."""

        built: TestTheSchedule.RecordingSchedule | None = None

        def __init__(self, queue, registry, *, interval_seconds: float) -> None:
            self.interval = interval_seconds
            self.registry = registry
            self.swept = False
            self.stopped = False
            self._stopping = asyncio.Event()
            TestTheSchedule.RecordingSchedule.built = self

        async def run(self) -> None:
            self.swept = True
            await self._stopping.wait()

        def request_stop(self) -> None:
            self.stopped = True
            self._stopping.set()

    class DeafSchedule(RecordingSchedule):
        """A schedule that hears the request and carries on regardless."""

        def request_stop(self) -> None:
            self.stopped = True

    class BrokenSchedule(RecordingSchedule):
        """A schedule whose own loop ended on an exception."""

        async def run(self) -> None:
            self.swept = True
            raise RuntimeError("the schedule ended badly")

    @staticmethod
    def _worker_that(run) -> type:
        class StubWorker:
            def __init__(self, queue, handlers, **timings) -> None:
                pass

            async def run(self) -> None:
                await run()

        return StubWorker

    async def _run_worker(
        self, monkeypatch, run, schedule: type = RecordingSchedule
    ) -> TestTheSchedule.RecordingSchedule:
        monkeypatch.setattr(worker, "IntervalScheduler", schedule)
        monkeypatch.setattr(worker, "JobWorker", self._worker_that(run))
        self.RecordingSchedule.built = None
        try:
            await worker.run_worker()
        finally:
            built = self.RecordingSchedule.built
        assert built is not None
        return built

    @staticmethod
    async def _returns() -> None:
        """A poll loop that stops the moment it is started."""
        return

    @pytest.mark.parametrize("configured", [137.0, 0.0])
    async def test_it_runs_beside_the_loop_with_the_configured_interval(
        self, monkeypatch, configured: float
    ) -> None:
        """And it is put down when the loop returns: ``run_worker`` ends when
        the worker does, without waiting an interval out to do it.

        A number the configuration does not hold by default, because comparing
        the schedule against ``sync_config().incremental_interval`` compares the
        implementation with itself: both sides are ``0.0`` for a worker that
        reads the config and for one that hardcodes a zero, and the wire from
        ``SyncConfig`` to the schedule would be untested. ``0.0`` stays as a
        second case because it is the shipped value.
        """
        monkeypatch.setattr(
            worker, "sync_config", lambda: SyncConfig(incremental_interval=configured)
        )

        schedule = await self._run_worker(monkeypatch, self._returns)

        assert schedule.interval == configured
        assert schedule.swept, "the schedule was built but never started"
        assert schedule.stopped, "a schedule left running would outlive its SIGTERM"

    async def test_a_loop_that_dies_still_puts_the_schedule_down(
        self, monkeypatch
    ) -> None:
        """Otherwise a worker that crashed leaves a task queueing jobs that
        nothing will ever claim."""

        async def die() -> None:
            raise RuntimeError("the loop went away")

        with pytest.raises(RuntimeError, match="loop went away"):
            await self._run_worker(monkeypatch, die)

        assert self.RecordingSchedule.built is not None
        assert self.RecordingSchedule.built.stopped

    async def test_a_schedule_that_will_not_stop_is_cancelled(
        self, monkeypatch, caplog
    ) -> None:
        """The guarantee behind the request: a sweep wedged in a database call
        must not hold up a process that has been told to exit."""
        monkeypatch.setattr(worker, "STOP_GRACE_SECONDS", 0.01)

        schedule = await self._run_worker(monkeypatch, self._returns, self.DeafSchedule)

        assert schedule.stopped, "it was asked first, and only then cancelled"
        assert "did not stop" in caplog.text

    async def test_a_cancellation_of_the_worker_itself_is_not_swallowed(
        self, monkeypatch
    ) -> None:
        """Two cancellations meet here and only one of them is ours to eat.

        ``_stopped`` runs from a ``finally``, so a supervisor tearing the
        process down can cancel ``run_worker`` while it waits on the schedule.
        Swallowing that would have ``run_worker`` return normally and leave the
        caller waiting for a signal that never arrives.
        """
        monkeypatch.setattr(worker, "STOP_GRACE_SECONDS", 0.0)

        async def slow_to_die() -> None:
            """Cancelled at once by the grace of zero, then takes a moment."""
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(30.0)

        # `asyncio.timeout` is the supervisor here: it cancels this task while
        # `_stopped` sits on `await task`, which is the one place the two
        # cancellations are indistinguishable without asking whose it is. A
        # `TimeoutError` on the way out is proof the cancellation got through;
        # swallowed, the block would simply end.
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await worker._stopped(asyncio.create_task(slow_to_die()))

    async def test_a_schedule_that_ended_badly_is_logged_and_not_raised(
        self, monkeypatch, caplog
    ) -> None:
        """This is awaited from a ``finally``; raising here would replace the
        reason the worker actually stopped with a footnote about the schedule
        — and hide it, if the worker stopped because of a crash of its own."""
        await self._run_worker(monkeypatch, self._returns, self.BrokenSchedule)

        assert "The incremental schedule ended badly" in caplog.text


async def test_the_handler_opens_the_mailbox_the_job_names(
    session_factory, encryption_key, registry
) -> None:
    async with session_factory() as session:
        account_id = await stored_account(session)
    engine = RecordingEngine()
    handler = worker.build_handlers(engine, registry, session_factory)[JobKind.IMPORT]

    await handler(a_job(account_id), RecordingQueue())

    source, target, mode = engine.calls[0]
    assert isinstance(source, FakeSource)
    assert source.secret == MAILBOX, "the credential never reached the provider"
    assert source.closed, "the handler owns the source and has to close it"
    assert (target.account_id, target.address, target.provider) == (
        account_id,
        ADDRESS,
        MailProvider.FAKE,
    )


async def test_a_page_tally_lands_in_the_job_row(
    session_factory, encryption_key, registry
) -> None:
    """Skipped counts as done: the bar measures the mailbox, not the writing."""
    async with session_factory() as session:
        account_id = await stored_account(session)
    handler = worker.build_handlers(RecordingEngine(), registry, session_factory)[
        JobKind.IMPORT
    ]
    queue = RecordingQueue()

    await handler(a_job(account_id, job_id=7), queue)

    assert queue.reports == [(7, 3, 1, 9)]


async def test_the_mailbox_is_closed_even_when_the_import_blows_up(
    session_factory, encryption_key, registry
) -> None:
    async with session_factory() as session:
        account_id = await stored_account(session)
    engine = RecordingEngine(explode=True)
    handler = worker.build_handlers(engine, registry, session_factory)[JobKind.IMPORT]

    with pytest.raises(RuntimeError, match="graph went away"):
        await handler(a_job(account_id), RecordingQueue())

    assert isinstance(engine.calls[0][0], FakeSource)
    assert engine.calls[0][0].closed


async def test_an_account_without_a_credential_asks_for_a_human(
    session_factory, encryption_key, registry
) -> None:
    """An unfinished setup is an auth failure, so the loop stops retrying it."""
    async with session_factory() as session:
        account_id = await stored_account(session, with_credential=False)
    handler = worker.build_handlers(RecordingEngine(), registry, session_factory)[
        JobKind.IMPORT
    ]

    with pytest.raises(MailAuthError, match="no stored credential"):
        await handler(a_job(account_id), RecordingQueue())


async def test_a_job_that_names_no_account_says_so(
    session_factory, encryption_key, registry
) -> None:
    handler = worker.build_handlers(RecordingEngine(), registry, session_factory)[
        JobKind.IMPORT
    ]

    with pytest.raises(LookupError, match="names no account"):
        await handler(a_job(None), RecordingQueue())


async def test_a_job_whose_account_is_gone_says_so(
    session_factory, encryption_key, registry
) -> None:
    handler = worker.build_handlers(RecordingEngine(), registry, session_factory)[
        JobKind.IMPORT
    ]

    with pytest.raises(LookupError, match="which is gone"):
        await handler(a_job(4711), RecordingQueue())


class TestTheIncrementalJob:
    """The kind a sweep queues: the same handler with the mode changed.

    What a delta *is* — where it resumes, what it does with an expired cursor,
    that it fetches exactly the one new message — belongs to the engine and is
    settled against real fixtures there. Two things are only true here: that a
    job of this kind asks for a delta and one of kind ``import`` does not, and
    what happens to the derived layer afterwards.
    """

    @staticmethod
    def _handlers(
        engine: RecordingEngine,
        registry: ProviderRegistry,
        session_factory: SessionFactory,
    ) -> dict[JobKind, JobHandler]:
        return worker.build_handlers(engine, registry, session_factory)

    async def test_the_two_kinds_ask_the_engine_for_different_things(
        self, session_factory, encryption_key, registry
    ) -> None:
        """The whole difference between them, and the only one."""
        async with session_factory() as session:
            account_id = await stored_account(session)
        engine = RecordingEngine()
        handlers = self._handlers(engine, registry, session_factory)

        await handlers[JobKind.IMPORT](a_job(account_id), RecordingQueue())
        await handlers[JobKind.INCREMENTAL](a_delta_job(account_id), RecordingQueue())

        assert engine.modes == [SyncCursorKind.FULL, SyncCursorKind.INCREMENTAL]

    async def test_it_opens_the_mailbox_the_way_an_import_does(
        self, session_factory, encryption_key, registry
    ) -> None:
        """One path, not two: a second one would be the place the credential
        is forgotten, and an unattended job is where nobody would notice."""
        async with session_factory() as session:
            account_id = await stored_account(session)
        engine = RecordingEngine()
        handler = self._handlers(engine, registry, session_factory)[JobKind.INCREMENTAL]

        await handler(a_delta_job(account_id), RecordingQueue())

        source, target, _ = engine.calls[0]
        assert isinstance(source, FakeSource)
        assert source.secret == MAILBOX, "the credential never reached the provider"
        assert source.closed, "the handler owns the source and has to close it"
        assert target.account_id == account_id

    async def test_new_mail_queues_a_rebuild_of_the_derived_layer(
        self, session_factory, encryption_key, registry
    ) -> None:
        """Deliberately the whole rebuild and not an incremental recomputation:
        the rebuild is idempotent by construction, and none of the three
        analyses is local to the messages that just arrived."""
        async with session_factory() as session:
            account_id = await stored_account(session)
        handler = self._handlers(
            RecordingEngine(archived=1), registry, session_factory
        )[JobKind.INCREMENTAL]
        queue = RecordingQueue()

        await handler(a_delta_job(account_id), queue)

        assert queue.queued == [(JobKind.DERIVE, None)]

    async def test_a_delta_that_archived_nothing_queues_nothing(
        self, session_factory, encryption_key, registry
    ) -> None:
        """The normal outcome of a sweep, and rebuilding the whole derived
        layer over no new mail every interval is how a laptop finds its fan."""
        async with session_factory() as session:
            account_id = await stored_account(session)
        handler = self._handlers(
            RecordingEngine(archived=0), registry, session_factory
        )[JobKind.INCREMENTAL]
        queue = RecordingQueue()

        await handler(a_delta_job(account_id), queue)

        assert queue.queued == []

    async def test_a_rebuild_that_is_already_open_is_not_queued_twice(
        self, session_factory, encryption_key, registry
    ) -> None:
        """A rebuild starts by deleting the derived layer, so two of them
        interleaving wipe rows the other has already written."""
        async with session_factory() as session:
            account_id = await stored_account(session)
        handler = self._handlers(
            RecordingEngine(archived=3), registry, session_factory
        )[JobKind.INCREMENTAL]
        queue = RecordingQueue(already_open=JobKind.DERIVE)

        await handler(a_delta_job(account_id), queue)

        assert queue.queued == []

    async def test_a_cancelled_delta_queues_nothing(
        self, session_factory, encryption_key, registry
    ) -> None:
        """A human who pressed stop asked for work to end, not for an hour of
        graph writes to begin."""
        async with session_factory() as session:
            account_id = await stored_account(session)
        engine = RecordingEngine(archived=3, cancelled=True)
        handler = self._handlers(engine, registry, session_factory)[JobKind.INCREMENTAL]
        queue = RecordingQueue()

        await handler(a_delta_job(account_id), queue)

        assert queue.queued == []

    async def test_a_stop_pressed_during_the_last_page_queues_nothing_either(
        self, session_factory, encryption_key, registry
    ) -> None:
        """The run never saw the cancel; the row did, and the row is the truth.

        A delta is usually one page, and the page loop breaks on "no next
        cursor" before it asks whether to stop — so a human who pressed stop
        gets a run reporting `cancelled=False`. The queue then ends the job as
        cancelled anyway, and without this the same press would have started an
        hour of graph writes on its way out.
        """
        async with session_factory() as session:
            account_id = await stored_account(session)
        engine = RecordingEngine(archived=3, cancelled=False)
        handler = self._handlers(engine, registry, session_factory)[JobKind.INCREMENTAL]
        queue = CancellingQueue()

        await handler(a_delta_job(account_id), queue)

        assert queue.queued == []

    async def test_a_rebuild_that_cannot_be_queued_does_not_fail_the_import(
        self, session_factory, encryption_key, registry, caplog
    ) -> None:
        """The mail is archived and the watermark has moved by the time this runs.

        A locked database here would otherwise leave the row saying the sync
        failed, with ``max_attempts=1`` and no retry. A rebuild that was not
        queued costs one button press; a job wrongly marked failed costs trust
        in the job list.
        """
        async with session_factory() as session:
            account_id = await stored_account(session)
        handler = self._handlers(
            RecordingEngine(archived=1), registry, session_factory
        )[JobKind.INCREMENTAL]

        await handler(a_delta_job(account_id), RefusingQueue())

        assert "Could not queue the rebuild" in caplog.text

    async def test_a_full_import_queues_no_rebuild(
        self, session_factory, encryption_key, registry
    ) -> None:
        """A human started it and is watching it, on the page that offers the
        rebuild button; a sweep at three in the morning has nobody to press."""
        async with session_factory() as session:
            account_id = await stored_account(session)
        handler = self._handlers(
            RecordingEngine(archived=9), registry, session_factory
        )[JobKind.IMPORT]
        queue = RecordingQueue()

        await handler(a_job(account_id), queue)

        assert queue.queued == []

    async def test_a_delta_that_blew_up_queues_no_rebuild(
        self, session_factory, encryption_key, registry
    ) -> None:
        """Half a mailbox reached the graph; recomputing what the archive means
        over half a mailbox is worse than not recomputing it."""
        async with session_factory() as session:
            account_id = await stored_account(session)
        engine = RecordingEngine(explode=True)
        handler = self._handlers(engine, registry, session_factory)[JobKind.INCREMENTAL]
        queue = RecordingQueue()

        with pytest.raises(RuntimeError, match="graph went away"):
            await handler(a_delta_job(account_id), queue)

        assert queue.queued == []
        source = engine.calls[0][0]
        assert isinstance(source, FakeSource)
        assert source.closed, "the mailbox is still closed"


def test_the_worker_process_carries_no_ui_framework() -> None:
    """§4.1's promise, checked rather than trusted: `app.worker` reaches
    `app.configuration` and `app.composition`, and neither pulls Reflex in."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"importing app.worker failed:\n{result.stderr}"
    offenders = set(result.stdout.strip().split(",")) & set(FORBIDDEN)
    assert not offenders, (
        f"the worker process dragged in {sorted(offenders)} — it renders nothing "
        "and a second interpreter's worth of Reflex is pure overhead"
    )


class RotatingCredentials:
    """What a provider hands back after Google reissued its refresh token."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def to_secret(self) -> str:
        return self._secret


class RotatingSource(FakeSource):
    """A mailbox whose credentials change while it is being read.

    Gmail's do: a re-consent and the idle-expiry path both reissue the refresh
    token mid-run, and the new one arrives silently. `GmailSource.credentials`
    is where it surfaces, and this is the shape of that attribute.
    """

    rotated_to: str | None = None

    @property
    def credentials(self) -> RotatingCredentials | None:
        if self.rotated_to is None:
            return None
        return RotatingCredentials(self.rotated_to)


class TestADeltaAllTheWayThrough:
    """§10, phase 7's definition of done, with nothing standing in for anything.

    Both halves of that sentence — "fetches exactly the one new mail" *and*
    "recomputes the derived nodes" — are otherwise proven apart:
    ``TestTheIncrementalJob`` runs the real handler against a stand-in engine,
    and ``test_engine_delta.py`` runs the real engine against a stand-in queue.
    A defect that lives only in the join, such as a result computed correctly
    and then handed on stale, is invisible to both.

    The graph is the one thing still stood in for: no FalkorDB starts in this
    suite, and the writer that fills it is exercised for real in
    ``mailarc-core``'s own tests.
    """

    @staticmethod
    def _engine(tmp_path, session_factory, notes: GraphNotes) -> ImportEngine:
        archive = ArchiveConfig(store_dir=tmp_path / "blobs")
        return ImportEngine(
            config=SyncConfig(batch_size=2),
            blobs=BlobStore(archive),
            archiver=MessageArchiver(archive),
            graph_session=cast(GraphSessionFactory, lambda: one_graph_session(notes)),
            database_session=session_factory,
        )

    async def test_one_new_message_is_archived_and_queues_exactly_one_rebuild(
        self, tmp_path, database, session_factory, encryption_key
    ) -> None:
        mailbox = tmp_path / "mailbox"
        mailbox.mkdir()
        for number in (1, 2, 3):
            (mailbox / f"m{number}.eml").write_bytes(a_message(number))
        async with session_factory() as session:
            account_id = await stored_account(session, secret=str(mailbox))
        registry = ProviderRegistry()
        registry.register(FAKE_DESCRIPTOR, FakeMailSource.create)
        notes = GraphNotes()
        queue = JobQueue(session_factory)
        handlers = worker.build_handlers(
            self._engine(tmp_path, session_factory, notes), registry, session_factory
        )

        first = await queue.enqueue(JobKind.IMPORT, account_id)
        await handlers[JobKind.IMPORT](a_job(account_id, first), queue)
        (mailbox / "m4.eml").write_bytes(a_message(4))
        second = await queue.enqueue(JobKind.INCREMENTAL, account_id)
        await handlers[JobKind.INCREMENTAL](a_delta_job(account_id, second), queue)

        assert notes.messages() == [f"m{number}@example.com" for number in (1, 2, 3, 4)]
        async with session_factory() as session:
            archived = (
                (await session.execute(select(MailArchivedMessageEntity)))
                .scalars()
                .all()
            )
            rebuilds = (
                (
                    await session.execute(
                        select(MailSyncJobEntity).where(
                            MailSyncJobEntity.kind == JobKind.DERIVE
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(archived) == 4, "the delta brought exactly the one new message in"
        assert len(rebuilds) == 1, "one rebuild for one delta that found something"


class TestARotatedCredential:
    """Nothing re-reads the secret after a run, so this is the only chance.

    Losing it costs nothing today and an `auth_error` on the next unattended
    run — with no hint that a working credential was handed to us and dropped.
    """

    @staticmethod
    def _registry(source: RotatingSource) -> ProviderRegistry:
        built = ProviderRegistry()
        built.register(
            ProviderDescriptor(provider=MailProvider.FAKE, label="Folder"),
            cast(MailSourceFactory, lambda account, secret: source),
        )
        return built

    @staticmethod
    async def _secrets(session_factory: SessionFactory) -> list[str]:
        async with session_factory() as session:
            result = await session.execute(select(MailCredentialEntity))
            return [row.secret for row in result.scalars().all()]

    async def _run(
        self,
        session_factory: SessionFactory,
        source: RotatingSource,
        kind: JobKind = JobKind.IMPORT,
    ) -> None:
        async with session_factory() as session:
            account_id = await stored_account(session)
        handlers = worker.build_handlers(
            RecordingEngine(), self._registry(source), session_factory
        )
        job = a_job(account_id) if kind is JobKind.IMPORT else a_delta_job(account_id)
        await handlers[kind](job, RecordingQueue())

    async def test_a_scheduled_delta_stores_it_too(
        self, session_factory, encryption_key
    ) -> None:
        """The one that matters most, and the reason both kinds share a
        handler: a delta runs unattended, so a token dropped here is one
        nobody sees go until the next sweep reports ``auth_error``."""
        rotated = '{"refresh_token": "1//rotated-during-a-sweep"}'
        source = RotatingSource.__new__(RotatingSource)
        source.closed = False
        source.rotated_to = rotated

        await self._run(session_factory, source, JobKind.INCREMENTAL)

        assert await self._secrets(session_factory) == [rotated]

    async def test_it_is_written_back(self, session_factory, encryption_key) -> None:
        rotated = '{"refresh_token": "1//rotated-mid-run"}'
        source = RotatingSource.__new__(RotatingSource)
        source.closed = False
        source.rotated_to = rotated

        await self._run(session_factory, source)

        assert await self._secrets(session_factory) == [rotated]

    async def test_a_credential_that_did_not_move_is_left_alone(
        self, session_factory, encryption_key
    ) -> None:
        source = RotatingSource.__new__(RotatingSource)
        source.closed = False
        source.rotated_to = MAILBOX

        await self._run(session_factory, source)

        assert await self._secrets(session_factory) == [MAILBOX]

    async def test_a_provider_with_nothing_to_say_is_not_asked(
        self, session_factory, encryption_key, registry
    ) -> None:
        """FakeSource has no `credentials` at all; duck-typing must not raise."""
        async with session_factory() as session:
            account_id = await stored_account(session)
        handlers = worker.build_handlers(RecordingEngine(), registry, session_factory)

        await handlers[JobKind.IMPORT](a_job(account_id), RecordingQueue())

        assert await self._secrets(session_factory) == [MAILBOX]

    async def test_a_write_that_fails_does_not_fail_the_import(
        self, session_factory, encryption_key, caplog
    ) -> None:
        """The mail is already archived; a lost token costs one re-consent."""

        class Exploding(RotatingCredentials):
            def to_secret(self) -> str:
                raise RuntimeError("the credential could not be serialised")

        source = RotatingSource.__new__(RotatingSource)
        source.closed = False
        source.rotated_to = "unused"
        type(source).credentials = property(  # ty: ignore[invalid-assignment]
            lambda self: Exploding("unused")
        )
        try:
            await self._run(session_factory, source)
        finally:
            del type(source).credentials

        assert source.closed, "the mailbox is still closed"
