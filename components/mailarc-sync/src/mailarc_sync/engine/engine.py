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
written twice. A delta (§10, phase 7) is that same loop with a different
cursor and not a second one, for the same reason.

Three decisions the code cannot state on its own:

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

**A watermark is read when the walk starts and stored when it ends.** Where the
next delta begins is decided *before* anything is listed, and written only once
the walk is through. Both halves matter: reading late would leave a gap the size
of the run, and writing early would arm a delta for work that never landed. The
walk and the run are not the same thing — a full import routinely spans several
attempts — so the mark of the attempt that began it is parked under
:data:`PENDING_SCOPE` and inherited by whoever finishes.
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
from mailarc_core.mail.errors import (
    MailCursorExpired,
    MailPermanentError,
    MailTransientError,
)
from mailarc_core.mail.model import (
    LabelInfo,
    MailProvider,
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

INCREMENTAL_SCOPE = SyncCursorKind.INCREMENTAL.value
"""The checkpoint scope a delta resumes from — a watermark, never a page token.

Its own row beside :data:`FULL_SCOPE` rather than a shared one, and the table's
``UNIQUE(account_id, scope)`` is what makes that free. Sharing would be wrong
three times over: the two hold different alphabets (a listing page token
against a provider watermark), a finished full walk stores ``None`` and would
erase the watermark, and ``messages_seen`` would add two unrelated totals into
one column.
"""

PENDING_SCOPE = "full-pending"
"""Where a full walk parks the watermark it read before it listed anything.

The third scope, and it exists because a full walk can span several attempts
while :data:`INCREMENTAL_SCOPE` may only ever hold a mark a delta can safely
start at. A walk that is interrupted has covered nothing above the page it
stopped at, so its mark is not one — but it is still the *right* mark for
whoever finishes the walk, and there is nowhere else to keep it: the full-scope
row holds a page token, the incremental row holds the last finished walk's
mark, and neither can be borrowed without one of them meaning two things.

Cleared, not deleted, when the walk finishes: the row's ``NULL`` cursor is read
back as "nothing parked here", the same way :data:`FULL_SCOPE`'s ``NULL`` means
"start from the top".
"""

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
        mode: SyncCursorKind = SyncCursorKind.FULL,
        on_progress: ProgressHook | None = None,
        cancelled: CancelCheck | None = None,
    ) -> ImportResult:
        """Import what the source offers in this ``mode``, from its checkpoint.

        One loop for both modes, because the difference between them is
        entirely in the cursor: a full run lists the mailbox from its page
        token, a delta lists what changed since its watermark, and everything
        after the listing — the ledger filter, the semaphore, the single
        archive consumer, the batching, the failure rows — is the same work on
        the same values. A second loop would be the same code with one
        argument changed and two chances to fix a bug in.

        Three things the mode does decide:

        * **A delta with no checkpoint does nothing.** There is no history
          before a watermark, so the first incremental run stores where a later
          one may start and returns. Listing from ``None`` instead would walk
          the whole mailbox under the name of a delta.
        * **Only a full run checkpoints mid-walk.** The incremental scope may
          only ever hold a legal starting point, and neither a page token nor
          the watermark of a page whose successors are unarchived is one.
          Replaying a whole delta is cheap and idempotent; a half-written one
          would skip mail.
        * **An expired cursor becomes a full walk in this same run.** See the
          ``except`` below.

        Either way a finished, uncancelled run leaves an incremental starting
        point behind, so a full import is also what arms the first delta.

        Raises whatever the source raises from the taxonomy in
        :mod:`mailarc_core.mail.errors` — an auth error ends the job, and a
        transient one has already been retried :data:`MAX_FETCH_ATTEMPTS`
        times per slice, so one that reaches the caller means an outage
        rather than a hiccup and is not worth retrying again. Plus one that is
        nobody's mailbox: a :class:`RuntimeError` for a source that hands back
        the cursor it was given, which is an adapter's bug and would otherwise
        be an unkillable loop. The caller owns the source and closes it; a run
        does not.
        """
        started_at = datetime.now(UTC)
        run = _Run(target=target, labels=await self._label_map(source))
        counts = ImportCounts()
        cursor = await self._resume(source, target, mode)
        watermark = await self._starting_mark(source, target, mode, cursor=cursor)
        checkpointed = 0
        estimated: int | None = None
        stopped = False
        resynced = False
        logger.info(
            "Import started for account %d (%s), mode=%s, resuming=%s",
            target.account_id,
            target.address,
            mode.value,
            cursor is not None,
        )

        if mode is SyncCursorKind.INCREMENTAL and cursor is None:
            logger.info(
                "Account %d has no delta checkpoint; watermarking instead of walking",
                target.account_id,
            )
            await self._store_watermark(target, watermark, counts)
            await self._report(on_progress, target, counts)
            return ImportResult(
                account_id=target.account_id,
                counts=counts,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                mode=mode,
            )

        while True:
            try:
                page = await source.list_messages(cursor, limit=self._config.batch_size)
            except MailCursorExpired as expired:
                if mode is SyncCursorKind.FULL:
                    # Nothing left to fall back to: a full walk starts from the
                    # top, so a provider refusing *that* is refusing the walk
                    # itself, and retrying it here would be an endless loop.
                    raise
                # Deliberately without clearing the stale checkpoint. If this
                # fallback crashes, the old value is still there and the next
                # run falls back again — repetitive, but complete. Clearing it
                # would make the next run bootstrap at today's watermark and
                # lose everything between the dead cursor and now.
                #
                # The *full* scope's row is cleared here, in the same breath
                # as the new mark, and that pairing is load-bearing. It is
                # tempting to leave it: this walk checkpoints into it and would
                # replace whatever a cancelled import left there, and a
                # re-walked page costs listing calls and never mail because
                # `_unarchived` and the writer's get-before-add make it a
                # no-op. That reasoning holds only if the walk *reaches* a
                # checkpoint. It writes one every `checkpoint_every` messages
                # or when the walk ends, so a fallback that is itself cancelled
                # on its first page — or that dies on a transient outage —
                # leaves a page token from a *previous* import in the full
                # scope while the mark beside it has moved forward to now. The
                # next "resume import" would then start at that stale page,
                # walk only the tail below it, finish cleanly, and store the
                # new mark as the delta's starting point: every message above
                # that page and newer than the old mark would be in no walk and
                # in no future delta. Silent, permanent, and reported as
                # succeeded. Clearing the row costs one re-listing of a
                # mailbox we are about to re-list anyway.
                logger.warning(
                    "Account %d: %s — walking the whole mailbox instead",
                    target.account_id,
                    expired,
                )
                mode, cursor, resynced = SyncCursorKind.FULL, None, True
                await self._checkpoint(target, None, counts)
                await self._remember_mark(target, watermark)
                continue
            counts = counts.plus(await self._import_page(source, run, page))
            if _stuck_on(cursor, page.next_cursor):
                # Every source owes the engine a cursor that moves. One that
                # does not turns this loop into a spin: the same page listed
                # for ever, the provider's quota spent on it, and nothing
                # archived, because the ledger filter makes every replay a
                # no-op. Ending the run leaves the checkpoint and the mark
                # exactly where they were, so nothing is lost by stopping.
                raise RuntimeError(
                    f"{source.provider} listed a page whose next cursor is the"
                    " one it was given; the run stops rather than spin"
                )
            cursor = page.next_cursor
            if mode is SyncCursorKind.FULL and (
                counts.processed - checkpointed >= self._config.checkpoint_every
                or cursor is None
            ):
                await self._checkpoint(target, cursor, counts)
                checkpointed = counts.processed
            estimated = _estimate(estimated, page.estimated_total, counts.processed)
            await self._report(on_progress, target, counts, estimated)
            if cursor is None:
                break
            if cancelled is not None and await cancelled():
                stopped = True
                break

        if not stopped:
            # A cancelled run has not covered everything before its watermark,
            # so it must replay; only a finished one may move the mark.
            await self._store_watermark(target, watermark, counts)
            if watermark is not None and mode is SyncCursorKind.FULL:
                # The parked mark has done its job. Left behind, the *next* full
                # import of this account would inherit a mark from this one.
                await self._checkpoint(target, None, counts, scope=PENDING_SCOPE)
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
            mode=mode,
            resynced=resynced,
        )

    async def _report(
        self,
        on_progress: ProgressHook | None,
        target: ImportTarget,
        counts: ImportCounts,
        estimated_total: int | None = None,
    ) -> None:
        """Say where the run stands, if the caller asked to be told.

        Every run reports at least once, a delta that found nothing included:
        a job row whose counters never move is indistinguishable from a job
        that hung.
        """
        if on_progress is None:
            return
        await on_progress(
            ImportProgress(
                account_id=target.account_id,
                counts=counts,
                estimated_total=estimated_total,
            )
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
        self, source: MailSourcePort, target: ImportTarget, mode: SyncCursorKind
    ) -> SyncCursor | None:
        """The cursor this mode's last run left, or ``None`` to start over.

        The mode names its own scope, so the two never read each other's
        cursor: a page token fed to a delta is a 404 and a watermark fed to a
        listing is a 400. ``NULL`` there is the finished full walk saying
        "start from the top", which :meth:`_stored_cursor` is careful to tell
        apart from an empty token.
        """
        return await self._stored_cursor(target, mode.value, source.provider, mode)

    async def _starting_mark(
        self,
        source: MailSourcePort,
        target: ImportTarget,
        mode: SyncCursorKind,
        *,
        cursor: SyncCursor | None,
    ) -> SyncCursor | None:
        """Where a later delta may begin — read once, when the *walk* began.

        For a delta, and for a full walk starting from the top, that is now: the
        mark is read before the first listing, so it sits behind everything the
        run goes on to fetch.

        For a full walk that *resumes*, now is the wrong answer and quietly so.
        A mailbox lists newest first, so a resumed attempt carries on downward
        into older mail — everything that arrived since the first attempt began
        sits above the page it picks up at and is in no page this walk will ever
        list. Storing today's mark on top of that would put those messages in no
        delta either, and an interrupted first import of a large mailbox is the
        ordinary path: a lease expires, a laptop closes, a human presses stop.
        So the mark is written down before the walk lists anything and inherited
        here, and a resume never asks the provider for a fresh one.

        A resume with nothing parked is a checkpoint from before this scope
        existed. Reading a fresh mark is all that is left, it is what every
        resume did before, and it happens once per account.
        """
        resuming = cursor is not None and mode is SyncCursorKind.FULL
        if resuming:
            inherited = await self._stored_cursor(
                target, PENDING_SCOPE, source.provider, SyncCursorKind.INCREMENTAL
            )
            if inherited is not None:
                return inherited
        mark = await source.watermark()
        if not resuming and mode is SyncCursorKind.FULL:
            await self._remember_mark(target, mark)
        return mark

    async def _remember_mark(
        self, target: ImportTarget, mark: SyncCursor | None
    ) -> None:
        """Park the walk's starting mark where a later attempt can inherit it.

        Written before the walk lists a page rather than after it, because the
        attempt that never reaches its own end is precisely the one whose mark
        the next attempt needs.
        """
        if mark is None:
            return
        await self._checkpoint(target, mark, ImportCounts(), scope=PENDING_SCOPE)

    async def _stored_cursor(
        self,
        target: ImportTarget,
        scope: str,
        provider: MailProvider,
        kind: SyncCursorKind,
    ) -> SyncCursor | None:
        """One scope's cursor, re-sealed as the kind that scope holds.

        ``cursor is None`` rather than a falsy test: an empty token is a real
        answer — a watermark meaning "nothing accounted for yet" — while
        ``NULL`` means the scope holds nothing at all.
        """
        async with self._database_session() as database:
            checkpoint = await self._checkpoints.find_by_account_and_scope(
                database, target.account_id, scope
            )
        if checkpoint is None or checkpoint.cursor is None:
            return None
        return SyncCursor(provider=provider, token=checkpoint.cursor, kind=kind)

    async def _checkpoint(
        self,
        target: ImportTarget,
        cursor: SyncCursor | None,
        counts: ImportCounts,
        *,
        scope: str = FULL_SCOPE,
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
                scope,
                cursor.token if cursor else None,
                counts.processed,
            )

    async def _store_watermark(
        self, target: ImportTarget, watermark: SyncCursor | None, counts: ImportCounts
    ) -> None:
        """Leave the point a later delta starts from — the start of *this* run.

        A start point and not an end point, and that is the whole reason
        :meth:`run` reads the watermark before it lists anything. Replaying
        from before the run is a no-op: ``_unarchived`` drops what the ledger
        already knows and the writer's get-before-add drops the rest. An end
        point would instead lose every message that arrived while the run was
        going, and lose it silently.

        ``None`` means this source has no delta at all, and then there is
        nothing to store — a row would be a promise the provider cannot keep,
        and the next scheduled delta would bootstrap off it forever.
        """
        if watermark is None:
            return
        await self._checkpoint(target, watermark, counts, scope=INCREMENTAL_SCOPE)

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


def _stuck_on(used: SyncCursor | None, following: SyncCursor | None) -> bool:
    """Whether a page handed back the very cursor it was listed with.

    The one invariant the engine cannot check inside a source and cannot live
    without: a cursor that does not move makes the page loop a spin. Compared
    by kind as well as token, because the two alphabets are unrelated and an
    equal token across kinds would be a coincidence rather than a repeat.
    """
    return (
        used is not None
        and following is not None
        and used.token == following.token
        and used.kind is following.kind
    )


def _estimate(so_far: int | None, page_total: int | None, processed: int) -> int | None:
    """The largest total seen yet, and never less than what is already done.

    A full walk reports the mailbox's size on every page, so the maximum is
    that number throughout. A history walk has no such number and reports its
    own page's size instead (Gmail's history replies carry no
    ``resultSizeEstimate``), which alone would have page five overwrite the
    total with a hundred while eight hundred are done — a job row reading
    ``800 / 100``. Taking the running maximum against what is processed keeps
    the two columns in a believable order without the engine having to know
    which endpoint it is walking.

    ``None`` stays ``None`` until some page offers a number, because
    :meth:`JobQueue.progress` reads that as "keep the estimate you have" — and
    inventing one from ``processed`` would show every such run at exactly 100%.
    """
    if page_total is None:
        return so_far
    return max(so_far or 0, page_total, processed)
