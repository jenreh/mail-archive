"""What the worker process makes of a job, and what it refuses to carry.

The poll loop belongs to ``mailarc_sync`` and is tested there. What is only
true here is the wiring: which kind of job runs which handler, and that a
handler turns the account row a job names into an open mailbox — credential
included — or says clearly why it cannot.

The last test is a subprocess, for the reason ``test_isolation.py`` gives: this
suite has Reflex in its interpreter already, so an in-process check that the
worker stays free of it would prove nothing.
"""

import subprocess
import sys
from collections.abc import AsyncIterator, Iterator

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import composition, worker
from mailarc_core.database.entities import (
    CredentialKind,
    MailAccountEntity,
    MailCredentialEntity,
)
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.model import MailProvider, ProviderDescriptor
from mailarc_sync.engine import (
    ImportCounts,
    ImportEngine,
    ImportProgress,
    ProviderRegistry,
)
from mailarc_sync.jobs import JobKind, JobState, SessionFactory, SyncJob

ADDRESS = "jens@example.com"
MAILBOX = "/mailboxes/exported"
"""The fake provider's credential is a directory path."""

FORBIDDEN = ("reflex", "appkit_mantine", "appkit_ui")
"""The worker renders nothing; carrying a UI framework would only cost memory."""

PROBE = """
import importlib, sys

importlib.import_module("app.worker")
print(",".join(sorted(sys.modules)))
"""


class FakeSource:
    """A mailbox that only remembers how it was opened and that it was closed."""

    provider = MailProvider.FAKE

    def __init__(self, account: MailAccountEntity, secret: str) -> None:
        self.address = account.email_address
        self.secret = secret
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class RecordingEngine:
    """Stands in for the pipeline: it says what it was asked to import.

    The pipeline itself runs against real fixtures in the engine's own tests.
    """

    def __init__(self, explode: bool = False) -> None:
        self.calls: list[tuple[FakeSource, object]] = []
        self._explode = explode

    async def run(self, source, target, *, on_progress=None, cancelled=None) -> None:
        self.calls.append((source, target))
        if on_progress is not None:
            await on_progress(
                ImportProgress(
                    account_id=target.account_id,
                    counts=ImportCounts(listed=4, skipped=1, archived=2, failed=1),
                    estimated_total=9,
                )
            )
        if self._explode:
            raise RuntimeError("the graph went away mid-run")


class RecordingQueue:
    """The job row, as far as a handler can see it."""

    def __init__(self) -> None:
        self.reports: list[tuple[int, int, int, int | None]] = []

    async def progress(
        self, job_id: int, done: int, failed: int, total: int | None = None
    ) -> None:
        self.reports.append((job_id, done, failed, total))

    async def is_cancel_requested(self, job_id: int) -> bool:
        return False


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
        FakeSource,
    )
    return built


async def stored_account(session: AsyncSession, with_credential: bool = True) -> int:
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
                account_id=account.id, kind=CredentialKind.PASSWORD, secret=MAILBOX
            )
        )
        await session.flush()
    return account.id


def a_job(account_id: int | None, job_id: int = 1) -> SyncJob:
    return SyncJob(
        id=job_id,
        kind=JobKind.IMPORT,
        state=JobState.RUNNING,
        account_id=account_id,
    )


def test_the_engine_is_built_from_the_registered_configuration() -> None:
    """Nothing is opened here — a store is a path until something writes."""
    engine = worker.build_engine()

    assert isinstance(engine, ImportEngine)
    assert engine._blobs.root == composition.archive_config().store_dir


def test_only_the_import_kind_has_a_handler_today() -> None:
    """Incremental is phase 7, derive phase 5, embed phase 6 — until then the
    loop fails such a job rather than pretending to run it."""
    handlers = worker.build_handlers(RecordingEngine(), ProviderRegistry())

    assert set(handlers) == {JobKind.IMPORT}


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
    assert seen["kinds"] == {JobKind.IMPORT}
    assert seen["worker_id"] == config.worker_id
    assert seen["lease_seconds"] == config.lease_seconds
    assert seen["heartbeat_seconds"] == config.heartbeat_interval
    assert seen["poll_seconds"] == config.poll_interval


async def test_the_handler_opens_the_mailbox_the_job_names(
    session_factory, encryption_key, registry
) -> None:
    async with session_factory() as session:
        account_id = await stored_account(session)
    engine = RecordingEngine()
    handler = worker.build_handlers(engine, registry, session_factory)[JobKind.IMPORT]

    await handler(a_job(account_id), RecordingQueue())

    source, target = engine.calls[0]
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
