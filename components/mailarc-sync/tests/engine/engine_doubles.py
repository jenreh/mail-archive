"""The stand-ins every engine test leans on, and the fixtures' building blocks.

Not a test module: it holds the graph session the writer is exercised against,
the family of mail sources that misbehave in one specific way each, and the
helpers that read a table back. Two test modules use them —
``test_engine_run.py`` for a full walk and ``test_engine_delta.py`` for an
incremental one — and a double that lived in one of them would have to be
imported out of a file pytest also collects.

Real everywhere it is cheap to be: real ``.eml`` files, the real parser, a real
blob store, a real SQLite file. The one stand-in is the graph session — no
FalkorDB runs here — and it is the same hand-written ``FakeSession`` shape
``tests/archive/test_archive_writer.py`` uses, so the writer is exercised
rather than mocked away.
"""

import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Never, cast

from sqlalchemy import select

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import Message
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.database.repositories import SyncCheckpointRepository
from mailarc_core.mail.errors import (
    MailAuthError,
    MailCursorExpired,
    MailTransientError,
)
from mailarc_core.mail.model import (
    MailProvider,
    MessagePage,
    MessageRef,
    RawMessage,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_sync.engine import engine as engine_module
from mailarc_sync.engine.config import SyncConfig
from mailarc_sync.engine.engine import GraphSessionFactory, ImportEngine
from mailarc_sync.engine.fake import FakeMailSource
from mailarc_sync.engine.model import ImportTarget

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

    async def watermark(self):  # noqa: ANN201 - delegates
        return await self._inner.watermark()

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


class ExpiredCursorSource(RecordingSource):
    """Rejects a delta's cursor the way Gmail rejects a stale `startHistoryId`.

    A full listing still works, which is the whole point: the mailbox is fine,
    only the shortcut into it has gone stale.
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.refused: list[str] = []

    async def list_messages(self, cursor, *, limit: int) -> MessagePage:
        if cursor is not None and cursor.kind is SyncCursorKind.INCREMENTAL:
            self.refused.append(cursor.token)
            raise MailCursorExpired("startHistoryId 42 is too old")
        return await self._inner.list_messages(cursor, limit=limit)


class AlwaysExpiredSource(RecordingSource):
    """Refuses every listing, delta or not — a provider that is simply broken.

    There is nothing left to fall back *to* once a full walk is refused, so
    this is what proves the fallback happens once and then gives up instead of
    listing forever.
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.list_calls = 0

    async def list_messages(self, cursor, *, limit: int) -> Never:
        self.list_calls += 1
        raise MailCursorExpired("this mailbox refuses every cursor")


class NoDeltaSource(RecordingSource):
    """A provider that cannot do deltas at all — the `None` watermark case.

    `FakeMailSource` can, since its file names give it an order, so the branch
    needs a double of its own rather than a mailbox that happens to be empty.
    """

    async def watermark(self) -> SyncCursor | None:
        return None


class LateArrivalSource(RecordingSource):
    """A message that lands *after* the last page of this run was listed.

    The case that decides whether a watermark is the start of a run or its
    end. This message is in no page this run will see, so a run watermarking
    at its own end would arm the next delta past it and lose it for good.
    """

    def __init__(self, directory: Path, *, arrival: int) -> None:
        super().__init__(directory)
        self._directory = directory
        self._arrival = arrival

    async def list_messages(self, cursor, *, limit: int) -> MessagePage:
        page = await self._inner.list_messages(cursor, limit=limit)
        if page.next_cursor is None:
            path = self._directory / f"m{self._arrival}.eml"
            path.write_bytes(message_bytes(self._arrival))
        return page


class NoAdvanceSource(RecordingSource):
    """Hands back the cursor it was just given — an adapter with a paging bug.

    The shape that turns the engine's page loop into a spin. Real enough to be
    worth a double: a `bisect_right` written as `bisect_left`, or a provider
    echoing its own page token, produces exactly this and nothing else notices,
    because the ledger filter makes every replay a silent no-op.
    """

    async def list_messages(self, cursor, *, limit: int) -> MessagePage:
        page = await self._inner.list_messages(cursor, limit=limit)
        return MessagePage(
            refs=page.refs,
            next_cursor=cursor
            or SyncCursor(
                provider=MailProvider.FAKE,
                token="stuck",  # noqa: S106 - a page token, not a secret
                kind=SyncCursorKind.FULL,
            ),
            estimated_total=page.estimated_total,
        )


class PagedTotalsSource(RecordingSource):
    """Reports each page's own size as the estimate, the way a history walk does.

    Gmail's ``users.history.list`` carries no ``resultSizeEstimate``, so its
    mapper answers with the page it just parsed. Read straight through, the
    second page would overwrite the total with a number smaller than what is
    already done.
    """

    async def list_messages(self, cursor, *, limit: int) -> MessagePage:
        page = await self._inner.list_messages(cursor, limit=limit)
        return MessagePage(
            refs=page.refs, next_cursor=page.next_cursor, estimated_total=len(page.refs)
        )


class MovingWatermarkSource(RecordingSource):
    """A mailbox whose watermark advances a step for every page listed.

    Gmail's ``historyId`` climbs with every message anyone sends the account, so
    it is a different number by the end of an import than it was at the start,
    and different again by the time an interrupted one is picked up. A folder of
    files cannot show that — its mark is a constant — and it is exactly that
    difference that decides whether an import loses the mail that arrived while
    it was running.

    Each read is recorded, so a test can also assert the engine did *not* ask.
    The last mark is held once the list runs out.
    """

    def __init__(self, directory: Path, *, marks: Sequence[str]) -> None:
        super().__init__(directory)
        self._marks = list(marks)
        self._at = 0
        self.marks_read: list[str] = []

    async def list_messages(self, cursor, *, limit: int) -> MessagePage:
        self._at = min(self._at + 1, len(self._marks) - 1)
        return await self._inner.list_messages(cursor, limit=limit)

    async def watermark(self) -> SyncCursor:
        token = self._marks[self._at]
        self.marks_read.append(token)
        return SyncCursor(
            provider=MailProvider.FAKE, token=token, kind=SyncCursorKind.INCREMENTAL
        )


@contextmanager
def graph_factory(session: FakeSession) -> Iterator[FakeSession]:
    """What the engine calls when it wants a graph session."""
    yield session


def build_engine(
    *, tmp_path: Path, database, graph: FakeSession, **overrides: Any
) -> ImportEngine:
    """The real engine, wired to the fixtures above."""
    archive = ArchiveConfig(store_dir=tmp_path / "blobs")
    config: dict[str, Any] = {"batch_size": 2, **overrides}
    return ImportEngine(
        config=SyncConfig(**config),
        blobs=BlobStore(archive),
        archiver=MessageArchiver(archive),
        graph_session=cast(GraphSessionFactory, lambda: graph_factory(graph)),
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


async def rows(database, entity: type[Any]) -> list[Any]:
    """Everything in one table, oldest first — the ledger as a human reads it."""
    async with database() as session:
        result = await session.execute(select(entity).order_by(entity.id))
        return list(result.scalars().all())


async def checkpoint(database, account_id: int, scope: str) -> Any:
    """One scope's checkpoint row, or `None` if that scope has never run."""
    async with database() as session:
        return await SyncCheckpointRepository().find_by_account_and_scope(
            session, account_id, scope
        )


async def seed_checkpoint(database, account_id: int, scope: str, cursor: str) -> None:
    """Put a run's leftovers in the table, the way a crash would have."""
    async with database() as session:
        await SyncCheckpointRepository().upsert_cursor(
            session, account_id, scope, cursor, 0
        )


def no_backoff(
    monkeypatch, delays: list[tuple[int, float | None]] | None = None
) -> None:
    """Keep the retry, drop the wait — the delay itself is tested separately."""

    def instant(attempt: int, retry_after: float | None) -> float:
        if delays is not None:
            delays.append((attempt, retry_after))
        return 0.0

    monkeypatch.setattr(engine_module, "_backoff_delay", instant)
