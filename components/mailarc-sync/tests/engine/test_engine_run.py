"""The whole pipeline, driven end to end against things that really are things.

Real everywhere it is cheap to be: a real :class:`FakeMailSource` over real
``.eml`` files, a real parser, a real blob store on ``tmp_path``, a real SQLite
file with the real repositories, and the real
:class:`~mailarc_core.archive.writer.MessageArchiver`. The one stand-in is the
graph session — no FalkorDB runs here — and it lives in ``engine_doubles.py``
beside this file, together with the fixtures in ``conftest.py``.

That leaves the claims below about the engine itself: which messages it decides
to fetch, when it writes a checkpoint, what it does with a message it cannot
parse, and whether a failure anywhere in the pipeline reaches the caller as the
error it was rather than as an ``ExceptionGroup`` or as a hang. The incremental
mode is next door in ``test_engine_delta.py``.
"""

import asyncio
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from engine_doubles import (
    ADDRESS,
    WITH_ATTACHMENT,
    ExplodingSession,
    FakeSession,
    HalfwaySource,
    LateFailureSource,
    NeverWorksSource,
    NoAdvanceSource,
    PagedTotalsSource,
    RecordingSource,
    RefusingSource,
    blob_root,
    build_engine,
    checkpoint,
    message_bytes,
    no_backoff,
    rows,
    source_for,
    target,
)
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import BlobKind
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.database.entities import (
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
    ParsedAttachment,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_sync.engine import engine as engine_module
from mailarc_sync.engine.config import SyncConfig
from mailarc_sync.engine.engine import (
    FULL_SCOPE,
    INCREMENTAL_SCOPE,
    PENDING_SCOPE,
    PERMANENT_REASON,
    GraphSessionFactory,
    ImportEngine,
)
from mailarc_sync.engine.fake import FakeMailSource
from mailarc_sync.engine.model import ImportCounts, ImportProgress


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


class TestTheEstimateTheJobRowShows:
    """`messages_done` may never overtake `messages_total`; a bar cannot show that."""

    async def test_a_per_page_estimate_never_falls_behind_what_is_done(
        self, mailbox, account_id, make_engine
    ) -> None:
        """One message a page, each page reporting `1`, three pages in all.

        Read straight through, the row would say ``3 / 1``. The running maximum
        says ``3 / 3``.
        """
        seen: list[ImportProgress] = []

        async def record(progress: ImportProgress) -> None:
            seen.append(progress)

        await make_engine(batch_size=1).run(
            PagedTotalsSource(mailbox), target(account_id), on_progress=record
        )

        assert [one.counts.processed for one in seen] == [1, 2, 3]
        assert [one.estimated_total for one in seen] == [1, 2, 3]

    def test_a_page_that_offers_no_number_leaves_the_estimate_alone(self) -> None:
        """`None` means "keep what the row has", not "you are at 100%"."""
        assert engine_module._estimate(None, None, 40) is None
        assert engine_module._estimate(12, None, 40) == 12


class TestASourceThatCannotPage:
    async def test_a_cursor_that_does_not_move_ends_the_run_instead_of_spinning(
        self, mailbox, account_id, make_engine
    ) -> None:
        """Otherwise the loop lists the same page until somebody kills the worker."""
        with pytest.raises(RuntimeError, match="next cursor"):
            await make_engine(batch_size=1).run(
                NoAdvanceSource(mailbox), target(account_id)
            )

    def test_a_cursor_of_another_kind_is_not_a_repeat(self) -> None:
        """The two alphabets are unrelated; an equal token across them is chance."""
        full = SyncCursor(
            provider=MailProvider.FAKE, token="7", kind=SyncCursorKind.FULL
        )
        delta = SyncCursor(
            provider=MailProvider.FAKE, token="7", kind=SyncCursorKind.INCREMENTAL
        )

        assert engine_module._stuck_on(full, delta) is False
        assert engine_module._stuck_on(full, full) is True
        assert engine_module._stuck_on(None, full) is False


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
        """One row per scope: the walk's own, the delta's start, the parked mark."""
        await make_engine().run(source_for(mailbox), target(account_id))

        checkpoints = await rows(database, MailSyncCheckpointEntity)
        assert {one.scope for one in checkpoints} == {
            FULL_SCOPE,
            INCREMENTAL_SCOPE,
            PENDING_SCOPE,
        }
        assert len(checkpoints) == 3, "one row per account and scope, updated in place"
        walk = await checkpoint(database, account_id, FULL_SCOPE)
        assert walk.messages_seen == 3
        assert walk.cursor is None

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
        """No resume point and no delta may advance past messages that never landed.

        The mark parked under `PENDING_SCOPE` is the exception and is not a
        position: it was written before the first listing and says where a
        *later* attempt should watermark from. A run that dies here leaves no
        full-scope cursor, so the next one starts from the top and overwrites
        it.
        """
        engine = build_engine(
            tmp_path=tmp_path, database=database, graph=ExplodingSession()
        )

        with pytest.raises(RuntimeError):
            await engine.run(source_for(mailbox), target(account_id))

        assert await checkpoint(database, account_id, FULL_SCOPE) is None
        assert await checkpoint(database, account_id, INCREMENTAL_SCOPE) is None
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

    def _engine(
        self, tmp_path, database, graph, opened, **overrides: Any
    ) -> ImportEngine:
        archive = ArchiveConfig(store_dir=tmp_path / "blobs")
        return ImportEngine(
            config=SyncConfig(**overrides),
            blobs=BlobStore(archive),
            archiver=MessageArchiver(archive),
            graph_session=cast(GraphSessionFactory, self._counting(graph, opened)),
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
