"""The import pipeline of §7.3 — list, filter, fetch, parse, store, archive.

```
list_messages(cursor)
  → drop what mail_archived_messages already knows
    → fetch_raw(slice)        semaphore, eight streams at once
      → parse + blob store    stdlib email and sha256 files, in a thread
        → MessageArchiver     one runic session, in a thread, ONE consumer
          → checkpoint        every checkpoint_every messages
```

There is no ``pipeline.py`` next to this file: the engine *is* the pipeline
(§3.1), and a second module would only be somewhere for the same loop to be
written twice.

Two decisions the code cannot state on its own:

**Exactly one archive consumer.** An :class:`asyncio.Queue` sits between the
fetch stage and the archive stage and gives backpressure — a slow graph slows
the fetching down instead of filling memory. Behind that queue there is one
consumer and never two: serialising FalkorDB writes is cheaper than
coordinating them, and a single writer is also what makes the get-before-add in
:class:`~mailarc_core.archive.writer.MessageArchiver` sound.

**No message disappears quietly.** A permanent failure travels the same queue as
a success and is written down by the same consumer, so skipping a message takes
a row in ``mail_failed_messages``. There is no ``except: pass`` here and no
place where one would fit.
"""

import asyncio
import logging
import math
import random
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from datetime import UTC, datetime

from runic.ogm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.model import ArchiveSource, BlobKind
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.database.repositories import (
    ArchivedMessageRepository,
    FailedMessageRepository,
    SyncCheckpointRepository,
)
from mailarc_core.mail.config import MailConfig
from mailarc_core.mail.errors import MailPermanentError, MailTransientError
from mailarc_core.mail.model import (
    LabelInfo,
    MessagePage,
    MessageRef,
    ParsedAttachment,
    RawMessage,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_core.mail.parsing import parse_message
from mailarc_core.mail.ports import MailSourcePort
from mailarc_sync.engine.config import SyncConfig
from mailarc_sync.engine.model import (
    ImportCounts,
    ImportProgress,
    ImportResult,
    ImportTarget,
    MessageFailure,
    PreparedMessage,
)

logger = logging.getLogger(__name__)

type ProgressHook = Callable[[ImportProgress], Awaitable[None]]
"""Told where the run stands after every page; the job row is the usual reader."""

type CancelCheck = Callable[[], Awaitable[bool]]
"""Asked between two pages whether to stop. A job is asked to stop, never killed."""

type GraphSessionFactory = Callable[[], AbstractContextManager[Session]]
"""Opens a runic session. Blocking, so the engine only calls it inside a thread."""

type DatabaseSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
"""Opens a relational session that commits when its block leaves cleanly."""

type FetchQueue = asyncio.Queue[PreparedMessage | MessageFailure | None]
"""What crosses between the stages: a message, a failure, or "that was all"."""

FULL_SCOPE = SyncCursorKind.FULL.value
"""The checkpoint scope a full walk of a mailbox resumes from."""

PERMANENT_REASON = "permanent"
"""What ``mail_failed_messages.reason`` says for the skip-and-continue case."""

MAX_FETCH_ATTEMPTS = 5
"""Tries per slice before a transient failure is allowed to end the run.

Not configuration: a provider still refusing after five backed-off attempts is
having an outage, and a run that waits it out holds a lease it cannot honour.
The job goes back to the queue and a later run resumes at the checkpoint.
"""

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0


class _Run:
    """The two things every stage of one run needs, carried together."""

    def __init__(
        self, *, target: ImportTarget, labels: Mapping[str, LabelInfo]
    ) -> None:
        self.target = target
        self.labels = labels

    def labels_for(self, ref: MessageRef) -> tuple[LabelInfo, ...]:
        """The labels of one message, named rather than identified.

        An id the provider did not list becomes a label of its own name:
        dropping it would be worse than showing an id, and a mailbox can gain a
        label between the listing and the fetch.
        """
        return tuple(
            self.labels.get(one, LabelInfo(provider_label_id=one, name=one))
            for one in ref.labels
        )


class _Tally:
    """Mutable counters, because two stages count into them at once.

    :class:`~mailarc_sync.engine.model.ImportCounts` is what leaves the engine;
    this is what the fetch stage and the archive consumer share while a page is
    still running.
    """

    def __init__(self, listed: int = 0) -> None:
        self.listed = listed
        self.skipped = 0
        self.archived = 0
        self.failed = 0

    def counts(self) -> ImportCounts:
        """The frozen snapshot of what this page did."""
        return ImportCounts(
            listed=self.listed,
            skipped=self.skipped,
            archived=self.archived,
            failed=self.failed,
        )


class ImportEngine:
    """Walks one mailbox through the port and leaves it in the archive.

    Everything it writes with is handed in: the composition root knows how to
    build a blob store, a graph session and a database session, and the engine
    only knows the order to use them in. That is also what lets the tests run
    the real pipeline against a directory of fixtures and a session that
    records instead of writing.
    """

    def __init__(
        self,
        *,
        config: SyncConfig,
        blobs: BlobStore,
        archiver: MessageArchiver,
        graph_session: GraphSessionFactory,
        database_session: DatabaseSessionFactory,
        mail_config: MailConfig | None = None,
    ) -> None:
        self._config = config
        self._blobs = blobs
        self._archiver = archiver
        self._graph_session = graph_session
        self._database_session = database_session
        self._mail_config = mail_config or MailConfig()
        self._archived = ArchivedMessageRepository()
        self._failed = FailedMessageRepository()
        self._checkpoints = SyncCheckpointRepository()
        self._slots = asyncio.Semaphore(config.fetch_concurrency)

    async def run(
        self,
        source: MailSourcePort,
        target: ImportTarget,
        *,
        on_progress: ProgressHook | None = None,
        cancelled: CancelCheck | None = None,
    ) -> ImportResult:
        """Import everything the source offers, resuming at the checkpoint.

        Raises whatever the source raises from the taxonomy in
        :mod:`mailarc_core.mail.errors` — an auth error ends the job, and a
        transient one has already been retried :data:`MAX_FETCH_ATTEMPTS`
        times per slice, so one that reaches the caller means an outage
        rather than a hiccup and is not worth retrying again. The
        caller owns the source and closes it; a run does not.
        """
        started_at = datetime.now(UTC)
        run = _Run(target=target, labels=await self._label_map(source))
        counts = ImportCounts()
        cursor = await self._resume(source, target)
        checkpointed = 0
        stopped = False
        logger.info(
            "Import started for account %d (%s), resuming=%s",
            target.account_id,
            target.address,
            cursor is not None,
        )

        while True:
            page = await source.list_messages(cursor, limit=self._config.batch_size)
            counts = counts.plus(await self._import_page(source, run, page))
            cursor = page.next_cursor
            if (
                counts.processed - checkpointed >= self._config.checkpoint_every
                or cursor is None
            ):
                await self._checkpoint(target, cursor, counts)
                checkpointed = counts.processed
            if on_progress is not None:
                await on_progress(
                    ImportProgress(
                        account_id=target.account_id,
                        counts=counts,
                        estimated_total=page.estimated_total,
                    )
                )
            if cursor is None:
                break
            if cancelled is not None and await cancelled():
                stopped = True
                break

        logger.info(
            "Import finished for account %d: %d archived, %d skipped, %d failed",
            target.account_id,
            counts.archived,
            counts.skipped,
            counts.failed,
        )
        return ImportResult(
            account_id=target.account_id,
            counts=counts,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            cursor=cursor.token if cursor else None,
            cancelled=stopped,
        )

    async def _import_page(
        self, source: MailSourcePort, run: _Run, page: MessagePage
    ) -> ImportCounts:
        """Fetch, parse and archive one page; report what it did.

        The two stages run side by side for the length of the page, and the
        queue between them is what keeps the fetching from outrunning the
        graph.
        """
        tally = _Tally(listed=len(page.refs))
        fresh = await self._unarchived(run.target, page.refs)
        tally.skipped = len(page.refs) - len(fresh)
        if not fresh:
            return tally.counts()

        queue: FetchQueue = asyncio.Queue(maxsize=self._config.fetch_concurrency)
        try:
            async with asyncio.TaskGroup() as group:
                # One consumer. Two would race for the same nodes and gain
                # nothing: the graph serialises the writes either way.
                group.create_task(self._archive_stage(queue, run, tally))
                group.create_task(self._fetch_stage(source, run, fresh, queue))
        except BaseExceptionGroup as failures:
            # The error taxonomy is this engine's contract with its caller, and
            # a caller that has to unwrap an ExceptionGroup to find a
            # MailAuthError will not. The group stays the cause, so a second
            # failure in another slice is still in the traceback.
            raise _first_error(failures) from failures
        return tally.counts()

    async def _fetch_stage(
        self,
        source: MailSourcePort,
        run: _Run,
        refs: Sequence[MessageRef],
        queue: FetchQueue,
    ) -> None:
        """Fetch every reference of a page and hand the results on.

        The sentinel goes in a ``finally`` so a failing fetch still ends the
        consumer instead of leaving it waiting for a message that is never
        coming.
        """
        try:
            async with asyncio.TaskGroup() as group:
                for part in _slices(refs, self._config.fetch_concurrency):
                    group.create_task(self._fetch_slice(source, run, part, queue))
        finally:
            await queue.put(None)

    async def _fetch_slice(
        self,
        source: MailSourcePort,
        run: _Run,
        refs: Sequence[MessageRef],
        queue: FetchQueue,
    ) -> None:
        """One slice of a page, retried as a whole minus what already arrived.

        The semaphore is held for the length of the stream, so eight
        conversations with a provider is the limit for this engine rather than
        for this page — which is what keeps it true if a caller ever runs two
        mailboxes through one engine.
        """
        async with self._slots:
            delivered: set[str] = set()
            for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
                pending = [
                    ref for ref in refs if ref.provider_message_id not in delivered
                ]
                if not pending:
                    return
                try:
                    async for raw in await source.fetch_raw(pending):
                        delivered.add(raw.ref.provider_message_id)
                        await queue.put(await self._prepare(raw, run))
                    return
                except MailTransientError as error:
                    if attempt == MAX_FETCH_ATTEMPTS:
                        raise
                    delay = _backoff_delay(attempt, error.retry_after)
                    logger.warning(
                        "Fetch attempt %d failed (%s), retrying in %.1fs",
                        attempt,
                        error,
                        delay,
                    )
                    await asyncio.sleep(delay)

    async def _prepare(
        self, raw: RawMessage, run: _Run
    ) -> PreparedMessage | MessageFailure:
        """Parse one message and put its bytes on disk, off the event loop.

        A :class:`MailPermanentError` becomes a value rather than an exception
        here: it is one message's problem, and the stage that owns the database
        session is the one that writes it down.
        """
        try:
            return await asyncio.to_thread(self._parse_and_store, raw, run)
        except MailPermanentError as error:
            logger.debug("Skipping message %s: %s", raw.ref.provider_message_id, error)
            return MessageFailure(
                provider_message_id=raw.ref.provider_message_id,
                reason=PERMANENT_REASON,
                detail=str(error),
            )

    def _parse_and_store(self, raw: RawMessage, run: _Run) -> PreparedMessage:
        """Blocking half of the fetch stage: parse, clean, write the blobs."""
        message = parse_message(raw.raw, config=self._mail_config)
        self._blobs.put(raw.raw, BlobKind.MESSAGE)
        attachments = tuple(self._store(one) for one in message.attachments)
        return PreparedMessage(
            source=ArchiveSource(
                account_id=str(run.target.account_id),
                account_address=run.target.address,
                provider=run.target.provider,
                provider_message_id=raw.ref.provider_message_id,
                provider_thread_id=raw.ref.provider_thread_id,
                labels=run.labels_for(raw.ref),
            ),
            message=message.model_copy(update={"attachments": attachments}),
        )

    def _store(self, attachment: ParsedAttachment) -> ParsedAttachment:
        """Put one attachment in the blob store and drop it from memory.

        The payload has a name on disk once this returns, and a page's worth of
        attachments waiting in a queue is megabytes nobody reads: the writer
        keys the node on the digest and never looks at the bytes.
        """
        if not attachment.payload:
            return attachment
        self._blobs.put(attachment.payload, BlobKind.ATTACHMENT)
        return attachment.model_copy(update={"payload": b""})

    async def _archive_stage(self, queue: FetchQueue, run: _Run, tally: _Tally) -> None:
        """The single consumer: graph first, then the relational ledger.

        It never raises out of its loop, and that is not caution. A consumer
        that raises while the fetch stage is blocked on a full queue deadlocks
        the page: the sentinel the fetch stage sends from its ``finally`` has
        nowhere to go, the cancellation has already been delivered, and not
        even an outer :func:`asyncio.timeout` gets the run back. So a write
        failure is remembered, the queue is drained, and the error is raised
        once the page is over — before any checkpoint has advanced.

        It flushes on ``batch_size`` alone. Flushing whenever the queue happens
        to be empty reads like a latency win and is the opposite: the fetch
        stage is network-bound, so the queue is empty most of the time, and the
        consumer would open a FalkorDB driver and a SQLite transaction per
        message instead of per batch. Whatever is left over is flushed once,
        after the sentinel.
        """
        buffer: list[PreparedMessage] = []
        failures: list[MessageFailure] = []
        error: BaseException | None = None

        while True:
            item = await queue.get()
            if item is None:
                break
            if error is not None:
                continue  # the run is over; drain so the fetch stage can end
            if isinstance(item, MessageFailure):
                failures.append(item)
            else:
                buffer.append(item)
            if len(buffer) + len(failures) >= self._config.batch_size:
                error = await self._try_flush(run, buffer, failures, tally)

        if error is None and (buffer or failures):
            error = await self._try_flush(run, buffer, failures, tally)
        if error is not None:
            raise error

    async def _try_flush(
        self,
        run: _Run,
        buffer: list[PreparedMessage],
        failures: list[MessageFailure],
        tally: _Tally,
    ) -> BaseException | None:
        """Write one batch, reporting a failure instead of raising it."""
        try:
            await self._flush(run, buffer, failures, tally)
        except Exception as error:  # noqa: BLE001 - re-raised once the page ends
            logger.error(
                "Archiving a batch of %d messages failed: %s", len(buffer), error
            )
            return error
        return None

    async def _flush(
        self,
        run: _Run,
        buffer: list[PreparedMessage],
        failures: list[MessageFailure],
        tally: _Tally,
    ) -> None:
        """One batch into the graph, then into SQLite, then forget it.

        The graph is written first on purpose. A crash between the two leaves a
        message archived but not noted, so the next run archives it again — and
        the writer is idempotent, so that costs one fetch and nothing else. The
        other order would lose the message for good.
        """
        if buffer:
            await asyncio.to_thread(self._write_batch, buffer)
        async with self._database_session() as database:
            if buffer:
                await self._archived.record_many(
                    database,
                    run.target.account_id,
                    {
                        one.source.provider_message_id: one.message.canonical_id
                        for one in buffer
                    },
                )
            for failure in failures:
                await self._failed.record(
                    database,
                    run.target.account_id,
                    failure.provider_message_id,
                    failure.reason,
                    failure.detail,
                )
        tally.archived += len(buffer)
        tally.failed += len(failures)
        buffer.clear()
        failures.clear()

    def _write_batch(self, batch: Sequence[PreparedMessage]) -> None:
        """The blocking graph write: one session, one commit, many messages."""
        with self._graph_session() as graph:
            for prepared in batch:
                self._archiver.archive(graph, prepared.message, prepared.source)

    async def _unarchived(
        self, target: ImportTarget, refs: Sequence[MessageRef]
    ) -> list[MessageRef]:
        """Drop the references this account already has, duplicates included.

        One statement per page. This is the whole reason
        ``mail_archived_messages`` exists: the graph cannot answer it for a
        batch, and asking it per message would be a round trip each. A provider
        that lists the same id twice in one page is filtered here too — the
        second copy would collide on the unique constraint rather than be
        recognised as the one we just wrote.
        """
        if not refs:
            return []
        async with self._database_session() as database:
            known = await self._archived.find_known_provider_ids(
                database,
                target.account_id,
                [ref.provider_message_id for ref in refs],
            )
        fresh: list[MessageRef] = []
        seen = set(known)
        for ref in refs:
            if ref.provider_message_id in seen:
                continue
            seen.add(ref.provider_message_id)
            fresh.append(ref)
        return fresh

    async def _resume(
        self, source: MailSourcePort, target: ImportTarget
    ) -> SyncCursor | None:
        """The cursor the last run stopped at, or ``None`` to start over."""
        async with self._database_session() as database:
            checkpoint = await self._checkpoints.find_by_account_and_scope(
                database, target.account_id, FULL_SCOPE
            )
        if checkpoint is None or not checkpoint.cursor:
            return None
        return SyncCursor(
            provider=source.provider,
            token=checkpoint.cursor,
            kind=SyncCursorKind.FULL,
        )

    async def _checkpoint(
        self, target: ImportTarget, cursor: SyncCursor | None, counts: ImportCounts
    ) -> None:
        """Write down where to pick up, once everything before it is archived.

        A finished walk stores ``None``: there is no next page, and a later run
        starts from the top and skips what it already has for the price of one
        listing pass.
        """
        async with self._database_session() as database:
            await self._checkpoints.upsert_cursor(
                database,
                target.account_id,
                FULL_SCOPE,
                cursor.token if cursor else None,
                counts.processed,
            )

    async def _label_map(self, source: MailSourcePort) -> Mapping[str, LabelInfo]:
        """Label ids to labels, read once per run.

        A message carries label *ids* — Gmail's are opaque strings like
        ``Label_12`` — so without this the graph would grow a node named after
        an identifier instead of after the name a human gave the label.
        """
        labels = await source.list_labels()
        return {one.provider_label_id: one for one in labels}


def _slices[T](items: Sequence[T], count: int) -> Iterator[Sequence[T]]:
    """Split a page into at most ``count`` slices of near-equal size."""
    if not items:
        return
    size = math.ceil(len(items) / max(count, 1))
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Seconds to wait before try number ``attempt + 1``.

    Exponential, capped, and jittered upwards only: the provider's own
    ``Retry-After`` is a floor the engine may exceed but never undercut, and the
    jitter keeps a hundred slices that failed together from returning together.
    """
    delay = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay + random.uniform(0.0, BACKOFF_BASE_SECONDS)  # noqa: S311 - jitter


def _first_error(failures: BaseExceptionGroup) -> BaseException:
    """The first real exception inside a (possibly nested) group."""
    first = failures.exceptions[0]
    return _first_error(first) if isinstance(first, BaseExceptionGroup) else first
