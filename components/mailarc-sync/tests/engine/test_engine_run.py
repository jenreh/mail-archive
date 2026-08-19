"""The whole pipeline, driven end to end against things that really are things.

Real everywhere it is cheap to be: a real :class:`FakeMailSource` over real
``.eml`` files, a real parser, a real blob store on ``tmp_path``, a real SQLite
file with the real repositories, and the real
:class:`~mailarc_core.archive.writer.MessageArchiver`. The one stand-in is the
graph session — no FalkorDB runs here — and it is the same hand-written
``FakeSession`` shape ``tests/archive/test_archive_writer.py`` uses, so the
writer is exercised rather than mocked away.

That leaves the claims below about the engine itself: which messages it decides
to fetch, when it writes a checkpoint, what it does with a message it cannot
parse, and whether a failure anywhere in the pipeline reaches the caller as the
error it was rather than as an ``ExceptionGroup`` or as a hang.
"""

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Never

import pytest
from appkit_commons.database.entities import Base
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import BlobKind, Message
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.database.entities import (
    MailAccountEntity,
    MailArchivedMessageEntity,
    MailFailedMessageEntity,
    MailSyncCheckpointEntity,
)
from mailarc_core.database.repositories import (
    ArchivedMessageRepository,
    FailedMessageRepository,
    SyncCheckpointRepository,
)
from mailarc_core.mail.errors import MailAuthError, MailTransientError
from mailarc_core.mail.model import (
    MailProvider,
    MessageRef,
    ParsedAttachment,
    RawMessage,
)
from mailarc_sync.engine import engine as engine_module
from mailarc_sync.engine.config import SyncConfig
from mailarc_sync.engine.engine import FULL_SCOPE, PERMANENT_REASON, ImportEngine
from mailarc_sync.engine.fake import FakeMailSource
from mailarc_sync.engine.model import ImportCounts, ImportProgress, ImportTarget

ADDRESS = "jens@example.com"

WITH_ATTACHMENT = (
    b"Message-ID: <m1@example.com>\n"
    b"Date: Wed, 04 Mar 2026 09:11:00 +0100\n"
    b"From: anna@example.com\n"
    b"To: jens@example.com\n"
    b"Subject: mit Anhang\n"
    b'Content-Type: multipart/mixed; boundary="b"\n'
    b"\n"
    b"--b\n"
    b"Content-Type: text/plain\n"
    b"\n"
    b"Anbei das Angebot.\n"
    b"--b\n"
    b'Content-Type: application/pdf; name="angebot.pdf"\n'
    b"Content-Disposition: attachment\n"
    b"\n"
    b"%PDF-1.4 not really a pdf\n"
    b"--b--\n"
)


def message_bytes(number: int) -> bytes:
    """One well-formed message; ``number`` is all that tells them apart."""
    return (
        f"Message-ID: <m{number}@example.com>\n"
        f"Date: Wed, 04 Mar 2026 09:{number:02d}:00 +0100\n"
        f"From: Anna <anna@example.com>\n"
        f"To: jens@example.com\n"
        f"Subject: Angebot {number}\n"
        "\n"
        "Hallo, anbei das Angebot.\n"
    ).encode()


class FakeSession:
    """The handful of :class:`runic.ogm.Session` members the writer reaches for.

    ``flush`` is what makes an added node findable by ``get``, the same
    behaviour the archive tests model and for the same reason: without it the
    writer's get-before-add would look like it works when it does not.
    """

    def __init__(self) -> None:
        self.nodes: dict[tuple[type, str], Any] = {}
        self.relate_calls: list[tuple[str, str, str]] = []
        self._pending: list[Any] = []

    def get(self, cls: type, pk: str) -> Any:
        return self.nodes.get((cls, pk))

    def add(self, entity: Any) -> None:
        self._pending.append(entity)

    def flush(self) -> None:
        for entity in self._pending:
            self.nodes[(type(entity), entity.id)] = entity
        self._pending.clear()

    def relate(self, source, field, target, edge=None) -> None:
        self.relate_calls.append((field.relationship, source.id, target.id))

    def messages(self) -> list[str]:
        """The canonical ids that reached the graph."""
        return sorted(pk for cls, pk in self.nodes if cls is Message)

    def edges(self, relationship: str) -> set[tuple[str, str]]:
        """Every edge of one type, as a real MERGE would leave them."""
        return {
            (source, target)
            for kind, source, target in self.relate_calls
            if kind == relationship
        }


class ExplodingSession(FakeSession):
    """A graph that refuses writes — an outage in the middle of a page.

    It takes its time about it, because the timing is the point: while the
    write hangs, the fetch stage fills the queue and blocks on it. That is the
    state the archive stage has to fail *out of* without wedging anyone.
    """

    def add(self, entity: Any) -> Never:
        time.sleep(0.2)
        raise RuntimeError("graph is gone")


class RecordingSource:
    """A :class:`FakeMailSource` that remembers what was asked of it."""

    provider = MailProvider.FAKE

    def __init__(self, directory: Path) -> None:
        self._inner = FakeMailSource(directory, address=ADDRESS)
        self.fetched: list[str] = []
        self.fetch_calls = 0

    async def verify(self):  # noqa: ANN201 - delegates to FakeMailSource
        return await self._inner.verify()

    async def list_labels(self):  # noqa: ANN201 - delegates
        return await self._inner.list_labels()

    async def list_messages(self, cursor, *, limit: int):  # noqa: ANN201 - delegates
        return await self._inner.list_messages(cursor, limit=limit)

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        self._record(refs)
        return await self._inner.fetch_raw(refs)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _record(self, refs: Sequence[MessageRef]) -> None:
        self.fetch_calls += 1
        self.fetched.extend(ref.provider_message_id for ref in refs)


class HalfwaySource(RecordingSource):
    """Dies with a rate limit after handing over the first message.

    The interesting half of a retry: what already arrived must not arrive
    twice, or the ledger's unique constraint turns a survivable outage into a
    failed job.
    """

    def __init__(self, directory: Path, *, retry_after: float | None = None) -> None:
        super().__init__(directory)
        self._retry_after = retry_after

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        self._record(refs)
        if self.fetch_calls == 1:
            return self._die_after_one(refs)
        return await self._inner.fetch_raw(refs)

    async def _die_after_one(
        self, refs: Sequence[MessageRef]
    ) -> AsyncIterator[RawMessage]:
        async for raw in await self._inner.fetch_raw(refs[:1]):
            yield raw
        raise MailTransientError("429 slow down", retry_after=self._retry_after)


class LateFailureSource(RecordingSource):
    """Rate-limits *after* the last message of the slice is already through."""

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        self._record(refs)
        if self.fetch_calls == 1:
            return self._die_at_the_end(refs)
        return await self._inner.fetch_raw(refs)

    async def _die_at_the_end(
        self, refs: Sequence[MessageRef]
    ) -> AsyncIterator[RawMessage]:
        async for raw in await self._inner.fetch_raw(refs):
            yield raw
        raise MailTransientError("429 on the way out")


class NeverWorksSource(RecordingSource):
    """A provider having an outage that outlasts the engine's patience."""

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> Never:
        self._record(refs)
        raise MailTransientError("503 service unavailable")


class RefusingSource(RecordingSource):
    """Credentials that stopped working between the listing and the fetch."""

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> Never:
        self._record(refs)
        raise MailAuthError("token revoked")


@pytest.fixture
def mailbox(tmp_path) -> Path:
    """Three messages in a directory, named so the listing order is known."""
    directory = tmp_path / "mailbox"
    directory.mkdir()
    for number in (1, 2, 3):
        (directory / f"m{number}.eml").write_bytes(message_bytes(number))
    return directory


@pytest.fixture
async def database(tmp_path) -> AsyncIterator[Any]:
    """A session factory over a real SQLite file, with appkit's commit rule.

    ``get_asyncdb_session`` commits when its block leaves cleanly and rolls
    back when it does not; the engine leans on exactly that, so the fixture
    copies it rather than handing out a bare session.
    """
    database_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail.db'}")
    async with database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(database_engine, expire_on_commit=False)

    @asynccontextmanager
    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as open_session:
            try:
                yield open_session
                await open_session.commit()
            except BaseException:
                await open_session.rollback()
                raise

    yield session
    await database_engine.dispose()


@pytest.fixture
async def account_id(database) -> int:
    """The account row every relational table hangs off."""
    async with database() as session:
        account = MailAccountEntity(
            provider=MailProvider.FAKE,
            display_name="Fixtures",
            email_address=ADDRESS,
        )
        session.add(account)
        await session.flush()
        return account.id


@pytest.fixture
def graph() -> FakeSession:
    return FakeSession()


@pytest.fixture
def make_engine(tmp_path, database, graph) -> Callable[..., ImportEngine]:
    """The real engine over the fixtures, with the knobs a test wants to turn."""

    def build(**overrides) -> ImportEngine:
        return build_engine(
            tmp_path=tmp_path, database=database, graph=graph, **overrides
        )

    return build


@contextmanager
def graph_factory(session: FakeSession) -> Iterator[FakeSession]:
    """What the engine calls when it wants a graph session."""
    yield session


def build_engine(
    *, tmp_path: Path, database, graph: FakeSession, **overrides
) -> ImportEngine:
    """The real engine, wired to the fixtures above."""
    archive = ArchiveConfig(store_dir=tmp_path / "blobs")
    return ImportEngine(
        config=SyncConfig(**{"batch_size": 2, **overrides}),
        blobs=BlobStore(archive),
        archiver=MessageArchiver(archive),
        graph_session=lambda: graph_factory(graph),
        database_session=database,
    )


def target(account_id: int) -> ImportTarget:
    return ImportTarget(
        account_id=account_id, address=ADDRESS, provider=MailProvider.FAKE
    )


def source_for(mailbox: Path) -> FakeMailSource:
    return FakeMailSource(mailbox, address=ADDRESS)


def blob_root(mailbox: Path) -> Path:
    """Where :func:`build_engine` puts the store — beside the mailbox."""
    return mailbox.parent / "blobs"


async def rows(database, entity: type) -> list[Any]:
    """Everything in one table, oldest first — the ledger as a human reads it."""
    async with database() as session:
        result = await session.execute(select(entity).order_by(entity.id))
        return list(result.scalars().all())


def no_backoff(
    monkeypatch, delays: list[tuple[int, float | None]] | None = None
) -> None:
    """Keep the retry, drop the wait — the delay itself is tested separately."""

    def instant(attempt: int, retry_after: float | None) -> float:
        if delays is not None:
            delays.append((attempt, retry_after))
        return 0.0

    monkeypatch.setattr(engine_module, "_backoff_delay", instant)


class TestAWholeMailbox:
    async def test_every_message_reaches_the_graph_the_disk_and_the_ledger(
        self, mailbox, database, account_id, graph, make_engine
    ) -> None:
        result = await make_engine().run(source_for(mailbox), target(account_id))

        assert result.counts.listed == 3
        assert result.counts.archived == 3
        assert result.counts.failed == 0
        assert graph.messages() == [
            "m1@example.com",
            "m2@example.com",
            "m3@example.com",
        ]
        blobs = BlobStore(ArchiveConfig(store_dir=blob_root(mailbox)))
        digest = hashlib.sha256(message_bytes(1)).hexdigest()
        assert blobs.exists(digest, BlobKind.MESSAGE), "the original .eml is on disk"
        assert len(await rows(database, MailArchivedMessageEntity)) == 3

    async def test_the_run_ends_with_no_cursor_and_was_not_cancelled(
        self, mailbox, account_id, make_engine
    ) -> None:
        result = await make_engine().run(source_for(mailbox), target(account_id))

        assert result.cursor is None
        assert result.cancelled is False
        assert result.finished_at >= result.started_at

    async def test_an_empty_mailbox_is_a_run_that_did_nothing(
        self, tmp_path, account_id, make_engine
    ) -> None:
        """A new account with nothing in it is not a failure to report."""
        empty = tmp_path / "empty"
        empty.mkdir()

        result = await make_engine().run(
            FakeMailSource(empty, address=ADDRESS), target(account_id)
        )

        assert result.counts == ImportCounts()
        assert result.cursor is None

    async def test_progress_is_reported_once_per_page(
        self, mailbox, account_id, make_engine
    ) -> None:
        """Two messages a page, so the caller hears 2 and then 3."""
        seen: list[ImportProgress] = []

        async def record(progress: ImportProgress) -> None:
            seen.append(progress)

        await make_engine().run(
            source_for(mailbox), target(account_id), on_progress=record
        )

        assert [one.counts.archived for one in seen] == [2, 3]
        assert seen[-1].estimated_total == 3


class TestWhatIsAlreadyOurs:
    async def test_a_message_in_the_ledger_is_never_fetched_again(
        self, mailbox, database, account_id, graph, make_engine
    ) -> None:
        """The whole reason `mail_archived_messages` exists."""
        async with database() as session:
            await ArchivedMessageRepository().record_many(
                session, account_id, {"m2": "m2@example.com"}
            )
        source = RecordingSource(mailbox)

        result = await make_engine().run(source, target(account_id))

        assert result.counts.skipped == 1
        assert result.counts.archived == 2
        assert "m2" not in source.fetched
        assert "m2@example.com" not in graph.messages()

    async def test_importing_the_same_mailbox_twice_fetches_nothing(
        self, mailbox, account_id, make_engine
    ) -> None:
        engine = make_engine()
        await engine.run(source_for(mailbox), target(account_id))

        source = RecordingSource(mailbox)
        second = await engine.run(source, target(account_id))

        assert second.counts.skipped == 3
        assert second.counts.archived == 0
        assert source.fetched == []


class TestCheckpoints:
    async def test_the_checkpoint_is_written_every_checkpoint_every_messages(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """One page per message and a checkpoint every two: page one has none."""
        seen: list[str | None] = []

        async def record(progress: ImportProgress) -> None:
            async with database() as session:
                checkpoint = await SyncCheckpointRepository().find_by_account_and_scope(
                    session, progress.account_id, FULL_SCOPE
                )
            seen.append(checkpoint.cursor if checkpoint else "none yet")

        await make_engine(batch_size=1, checkpoint_every=2).run(
            source_for(mailbox), target(account_id), on_progress=record
        )

        assert seen == ["none yet", "m3", None]

    async def test_the_last_checkpoint_counts_everything_the_run_processed(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        await make_engine().run(source_for(mailbox), target(account_id))

        checkpoints = await rows(database, MailSyncCheckpointEntity)
        assert len(checkpoints) == 1, "one row per account and scope, updated in place"
        assert checkpoints[0].messages_seen == 3
        assert checkpoints[0].cursor is None

    async def test_a_run_resumes_at_the_stored_cursor(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """The crash-and-restart case: the third message and nothing before it."""
        async with database() as session:
            await SyncCheckpointRepository().upsert_cursor(
                session, account_id, FULL_SCOPE, "m3", 2
            )
        source = RecordingSource(mailbox)

        result = await make_engine().run(source, target(account_id))

        assert result.counts.listed == 1
        assert source.fetched == ["m3"]


class TestABrokenMessage:
    async def test_it_leaves_a_row_and_the_run_carries_on(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """No `except: pass`: a skipped message is countable, not invisible."""
        (mailbox / "m2.eml").write_bytes(b"")

        result = await make_engine().run(source_for(mailbox), target(account_id))

        assert result.counts.failed == 1
        assert result.counts.archived == 2
        assert result.cursor is None, "the run walked the whole mailbox anyway"

        failures = await rows(database, MailFailedMessageEntity)
        assert [one.provider_message_id for one in failures] == ["m2"]
        assert failures[0].reason == PERMANENT_REASON
        assert failures[0].detail, "the row says what a human needs to judge it"
        assert failures[0].account_id == account_id

    async def test_the_broken_one_is_not_recorded_as_archived(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        (mailbox / "m2.eml").write_bytes(b"")

        await make_engine().run(source_for(mailbox), target(account_id))

        archived = await rows(database, MailArchivedMessageEntity)
        assert sorted(one.provider_message_id for one in archived) == ["m1", "m3"]

    async def test_two_broken_messages_leave_two_rows(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        (mailbox / "m1.eml").write_bytes(b"")
        (mailbox / "m3.eml").write_bytes(b"")

        result = await make_engine().run(source_for(mailbox), target(account_id))

        async with database() as session:
            assert await FailedMessageRepository().count(session) == 2
        assert result.counts.failed == 2
        assert result.counts.archived == 1


class TestTransientFailures:
    async def test_a_rate_limit_is_retried_and_the_run_finishes(
        self, mailbox, account_id, make_engine, monkeypatch
    ) -> None:
        delays: list[tuple[int, float | None]] = []
        no_backoff(monkeypatch, delays)
        source = HalfwaySource(mailbox, retry_after=42.0)

        result = await make_engine(batch_size=3, fetch_concurrency=1).run(
            source, target(account_id)
        )

        assert result.counts.archived == 3
        assert delays == [(1, 42.0)], "backed off once, told the provider's own floor"

    async def test_what_already_arrived_is_not_fetched_twice(
        self, mailbox, database, account_id, make_engine, monkeypatch
    ) -> None:
        """A retry that re-delivered would collide on the ledger's unique key."""
        no_backoff(monkeypatch)
        source = HalfwaySource(mailbox)

        await make_engine(batch_size=3, fetch_concurrency=1).run(
            source, target(account_id)
        )

        assert source.fetched == ["m1", "m2", "m3", "m2", "m3"]
        assert len(await rows(database, MailArchivedMessageEntity)) == 3

    async def test_a_failure_after_the_last_message_retries_nothing(
        self, mailbox, account_id, make_engine, monkeypatch
    ) -> None:
        """Everything arrived, then the provider complained: there is no work
        left to retry, and a loop that asked again would never end."""
        no_backoff(monkeypatch)
        source = LateFailureSource(mailbox)

        result = await make_engine(batch_size=3, fetch_concurrency=1).run(
            source, target(account_id)
        )

        assert result.counts.archived == 3
        assert source.fetch_calls == 1

    async def test_giving_up_after_the_last_attempt_ends_the_run(
        self, mailbox, account_id, make_engine, monkeypatch
    ) -> None:
        no_backoff(monkeypatch)
        source = NeverWorksSource(mailbox)

        with pytest.raises(MailTransientError):
            await make_engine(batch_size=3, fetch_concurrency=1).run(
                source, target(account_id)
            )

        assert source.fetch_calls == engine_module.MAX_FETCH_ATTEMPTS


class TestSlicingAPage:
    def test_a_page_is_spread_across_the_fetch_slots(self) -> None:
        """Ten references and eight slots: nobody waits on a slice of one."""
        assert [len(part) for part in engine_module._slices(list(range(10)), 8)] == [
            2,
            2,
            2,
            2,
            2,
        ]

    def test_a_short_page_uses_fewer_slices_than_slots(self) -> None:
        assert [len(part) for part in engine_module._slices([1, 2, 3], 8)] == [1, 1, 1]

    def test_an_empty_page_is_no_slices_at_all(self) -> None:
        assert list(engine_module._slices([], 8)) == []


class TestBackoffMaths:
    def test_it_grows_with_every_attempt(self) -> None:
        assert engine_module._backoff_delay(4, None) > engine_module._backoff_delay(
            1, None
        )

    def test_retry_after_is_a_floor_and_never_a_ceiling(self) -> None:
        """Jitter is added, never subtracted: undercutting earns another 429."""
        assert engine_module._backoff_delay(1, retry_after=30.0) >= 30.0

    def test_it_stops_growing_at_the_cap(self) -> None:
        ceiling = engine_module.BACKOFF_CAP_SECONDS + engine_module.BACKOFF_BASE_SECONDS

        assert engine_module._backoff_delay(20, None) <= ceiling


class TestFailuresThatEndTheRun:
    async def test_an_auth_error_reaches_the_caller_as_itself(
        self, mailbox, account_id, make_engine
    ) -> None:
        """`TaskGroup` reports an `ExceptionGroup`; the taxonomy is the contract.

        Under a timeout because the other way this can go wrong is a hang: the
        consumer is still waiting for messages the failed fetch will never send.
        """
        async with asyncio.timeout(10):
            with pytest.raises(MailAuthError):
                await make_engine().run(RefusingSource(mailbox), target(account_id))

    async def test_a_dead_graph_stops_the_run_instead_of_hanging(
        self, mailbox, database, account_id, tmp_path
    ) -> None:
        """The consumer drains rather than dies, or the fetch stage waits forever."""
        engine = build_engine(
            tmp_path=tmp_path, database=database, graph=ExplodingSession()
        )

        async with asyncio.timeout(10):
            with pytest.raises(RuntimeError):
                await engine.run(source_for(mailbox), target(account_id))

    async def test_a_dead_graph_does_not_wedge_a_queue_nobody_empties(
        self, mailbox, database, account_id, tmp_path
    ) -> None:
        """Where the deadlock would actually be: more messages than the queue
        holds, so the fetch stage is blocked on a ``put`` at the moment the
        archive stage gives up. A consumer that raised there would leave the
        producers waiting for a slot that never comes, and the run would never
        end — hence the drain."""
        for number in range(4, 24):
            (mailbox / f"m{number}.eml").write_bytes(message_bytes(number))
        engine = build_engine(
            tmp_path=tmp_path,
            database=database,
            graph=ExplodingSession(),
            batch_size=23,
            fetch_concurrency=2,
        )

        async with asyncio.timeout(10):
            with pytest.raises(RuntimeError):
                await engine.run(source_for(mailbox), target(account_id))

    async def test_nothing_is_written_down_when_the_graph_fails(
        self, mailbox, database, account_id, tmp_path
    ) -> None:
        """No checkpoint may advance past messages that never landed."""
        engine = build_engine(
            tmp_path=tmp_path, database=database, graph=ExplodingSession()
        )

        with pytest.raises(RuntimeError):
            await engine.run(source_for(mailbox), target(account_id))

        assert await rows(database, MailSyncCheckpointEntity) == []
        assert await rows(database, MailArchivedMessageEntity) == []


class TestCancelling:
    async def test_a_cancel_is_honoured_between_two_pages(
        self, mailbox, account_id, make_engine
    ) -> None:
        """A job is asked to stop, never killed mid-write."""

        async def stop_now() -> bool:
            return True

        result = await make_engine(batch_size=1).run(
            source_for(mailbox), target(account_id), cancelled=stop_now
        )

        assert result.cancelled is True
        assert result.counts.archived == 1
        assert result.cursor == "m2", "the next run picks up where this one stopped"


class TestWhatEndsUpOnTheEdges:
    async def test_a_label_reaches_the_graph_under_its_name(
        self, mailbox, account_id, graph, make_engine
    ) -> None:
        """Gmail sends label ids; a node called `Label_12` helps nobody."""
        await make_engine().run(source_for(mailbox), target(account_id))

        assert ("m1@example.com", f"{account_id}:INBOX") in graph.edges("LABELED")

    async def test_the_account_is_on_the_provenance_edge(
        self, mailbox, account_id, graph, make_engine
    ) -> None:
        await make_engine().run(source_for(mailbox), target(account_id))

        assert graph.edges("ARCHIVED_FROM") == {
            (f"m{number}@example.com", str(account_id)) for number in (1, 2, 3)
        }

    async def test_an_attachment_lands_in_the_store_and_on_an_edge(
        self, mailbox, account_id, graph, make_engine
    ) -> None:
        (mailbox / "m1.eml").write_bytes(WITH_ATTACHMENT)

        await make_engine().run(source_for(mailbox), target(account_id))

        stored = list(blob_root(mailbox).rglob("*.bin"))
        assert len(stored) == 1
        assert stored[0].read_bytes().startswith(b"%PDF-1.4")
        assert len(graph.edges("HAS_ATTACHMENT")) == 1

    def test_an_attachment_with_no_bytes_is_left_as_it_is(self, make_engine) -> None:
        """Nothing to store and nothing to drop — a part the parser could not read."""
        attachment = ParsedAttachment(filename="angebot.pdf")

        assert make_engine()._store(attachment) is attachment

    def test_the_payload_is_dropped_once_it_is_on_disk(
        self, mailbox, make_engine
    ) -> None:
        """A page of attachments waiting in the queue is memory nobody reads."""
        payload = b"%PDF-1.4 not really a pdf"
        attachment = ParsedAttachment(
            filename="angebot.pdf",
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            payload=payload,
        )

        light = make_engine()._store(attachment)

        assert light.payload == b""
        assert light.sha256 == attachment.sha256, "the digest is what the node keys on"
        assert BlobStore(ArchiveConfig(store_dir=blob_root(mailbox))).exists(
            attachment.sha256, BlobKind.ATTACHMENT
        )


class TestBatching:
    """One batch is one graph session and one relational transaction.

    The consumer used to flush whenever the queue happened to be empty, which
    on a network-bound fetch is nearly every message: forty messages opened
    twenty-five FalkorDB drivers. `batch_size` says what a batch is, and
    nothing else gets a vote.
    """

    @staticmethod
    def _counting(graph: FakeSession, opened: list[int]):
        @contextmanager
        def factory() -> Iterator[FakeSession]:
            opened.append(1)
            yield graph

        return factory

    def _engine(self, tmp_path, database, graph, opened, **overrides) -> ImportEngine:
        archive = ArchiveConfig(store_dir=tmp_path / "blobs")
        return ImportEngine(
            config=SyncConfig(**overrides),
            blobs=BlobStore(archive),
            archiver=MessageArchiver(archive),
            graph_session=self._counting(graph, opened),
            database_session=database,
        )

    async def test_a_page_that_fits_one_batch_opens_one_graph_session(
        self, tmp_path, database, graph, account_id
    ) -> None:
        mailbox = tmp_path / "many"
        mailbox.mkdir()
        for number in range(1, 21):
            (mailbox / f"m{number:03d}.eml").write_bytes(message_bytes(number))
        opened: list[int] = []

        result = await self._engine(
            tmp_path, database, graph, opened, batch_size=20, fetch_concurrency=4
        ).run(source_for(mailbox), target(account_id))

        assert result.counts.archived == 20
        assert len(opened) == 1, f"one batch, one session — got {len(opened)}"

    async def test_a_page_of_several_batches_opens_one_session_each(
        self, tmp_path, database, graph, account_id
    ) -> None:
        mailbox = tmp_path / "many"
        mailbox.mkdir()
        for number in range(1, 21):
            (mailbox / f"m{number:03d}.eml").write_bytes(message_bytes(number))
        opened: list[int] = []

        # Five per batch over a twenty-message page: four flushes, however the
        # fetch stage happens to interleave with the consumer.
        await self._engine(
            tmp_path, database, graph, opened, batch_size=5, fetch_concurrency=4
        ).run(source_for(mailbox), target(account_id))

        assert len(opened) == 4
