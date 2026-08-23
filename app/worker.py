"""The worker process: what a job kind means, and who can carry it out.

Here rather than in ``mailarc-sync`` because this is the layer that is allowed
to name implementations (§4.1). The engine walks a mailbox through a port and
must not be able to say "Gmail"; the loop in ``mailarc_sync.jobs.worker`` runs
whatever it is handed. So the wiring — which kind of job runs which handler,
where the credential comes from, which store the bytes land in — lives in the
composition root, and this module is the composition root of its own process.

The recurring *trigger* is wired here for the same reason. ``mailarc_sync``
owns an interval loop that enqueues (§7.2's "deliberately not a scheduler" cuts
both ways), but which accounts it may touch and how often is policy, and policy
belongs to the composition root — so :func:`run_worker` builds it, starts it
beside the poll loop and puts it down with it.

Started by :func:`app.composition.sync_worker_lifespan` on the desktop, or as
its own unit under Docker and systemd, and always as ``python -m app.worker``.
Deliberately lean: it reaches ``app.configuration``, ``app.composition``,
``app.derive`` and ``app.embedding``, and none of the four pulls Reflex in.
``tests/test_worker.py`` proves that from outside instead of trusting it.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from functools import partial
from typing import Any

from appkit_commons.database.session import get_asyncdb_session
from sqlalchemy.ext.asyncio import AsyncSession

from app import derive, embedding
from app.composition import (
    adopt_semantic_settings,
    archive_config,
    graph_config,
    mail_config,
    provider_registry,
    sync_config,
)
from app.configuration import configure
from mailarc_analytics import DerivedCounts, ProgressHook, RebuildProgress, RebuildStage
from mailarc_analytics.semantic import CancelCheck, EmbedProgress, EmbedRun
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.database.entities import CredentialKind
from mailarc_core.database.repositories import (
    MailAccountRepository,
    MailCredentialRepository,
)
from mailarc_core.graph.client import session as graph_session
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.model import MailProvider, SyncCursorKind
from mailarc_core.mail.ports import MailSourcePort
from mailarc_sync.engine import (
    ImportEngine,
    ImportProgress,
    ImportResult,
    ImportTarget,
    ProviderRegistry,
)
from mailarc_sync.jobs import (
    IntervalScheduler,
    JobHandler,
    JobKind,
    JobQueue,
    JobWorker,
    SessionFactory,
    SyncJob,
)

logger = logging.getLogger(__name__)

_ACCOUNTS = MailAccountRepository()
_CREDENTIALS = MailCredentialRepository()

type DerivedRebuild = Callable[[ProgressHook], DerivedCounts]
"""Runs one rebuild of the derived layer and says what it did. Blocking.

:func:`app.derive.rebuild` is the only implementation there is; it is a
parameter of :func:`build_handlers` for the reason
:class:`~app.composition.WorkerProcess` takes its command as one — so a test
can drive the handler's mechanics, the thread hop and the cancel included,
against something it controls. Nothing in the application passes it.
"""

type MessageEmbedding = Callable[[EmbedProgress, CancelCheck], Awaitable[EmbedRun]]
"""Embeds every message that still needs it and says what it did.

:func:`app.embedding.embed` is the only implementation there is; it is a
parameter of :func:`build_handlers` for the reason :data:`DerivedRebuild` is —
so a test can drive the handler's mechanics, the row and the cancel included,
against something it controls. Nothing in the application passes it.

Both hooks are non-optional here although the function they end up in accepts
``None`` for either: a job always has a row to report into and a flag to read,
and defaulting them would make "this handler forgot to wire the cancel" a thing
that runs.
"""

STOP_GRACE_SECONDS = 5.0
"""How long the schedule is given to notice the stop before it is cancelled.

Generous, because in the ordinary case it is not waited out at all: the flag
the schedule sits on is the one :meth:`IntervalScheduler.request_stop` sets, so
the task is already finished by the time this is measured. It only matters when
a sweep is inside a database call that will not return.
"""

_STAGES: tuple[RebuildStage, ...] = tuple(RebuildStage)
"""The rebuild's stages in order, which is the unit a derive job reports in.

``mail_sync_jobs`` counts messages, and a rebuild has none to count: it reads
the whole archive once and then walks all of it again per analysis, so
"messages done" would either sit at zero until the end or jump to the total
after the first stage and stay there. How many of the five stages are behind it
is the only number that moves forward exactly once per stage and never
backwards, which is what a progress bar is asking for.
"""


class _Stopped(Exception):
    """Raised in the rebuild's own thread when a human asked the job to stop.

    Private, and caught by the handler that armed it, so it never reaches the
    loop: §7.6's taxonomy is what the loop reads a failure through, and a
    cancel is not a failure. The rebuild is abandoned wherever it stands —
    safe to do, because the derived layer is disposable by construction and the
    next rebuild deletes what this one left behind before writing anything.
    """


def build_engine() -> ImportEngine:
    """The import pipeline, wired to this installation's stores."""
    archive = archive_config()
    return ImportEngine(
        config=sync_config(),
        blobs=BlobStore(archive),
        archiver=MessageArchiver(archive),
        graph_session=partial(graph_session, graph_config()),
        database_session=get_asyncdb_session,
        mail_config=mail_config(),
    )


def build_handlers(
    engine: ImportEngine,
    registry: ProviderRegistry,
    session_factory: SessionFactory = get_asyncdb_session,
    rebuild: DerivedRebuild = derive.rebuild,
    embed: MessageEmbedding = embedding.embed,
) -> dict[JobKind, JobHandler]:
    """Which kind of job runs what.

    One entry per :class:`JobKind`, and two of them are the same function.
    ``import`` walks a whole mailbox and ``incremental`` asks the same engine
    what changed since the last run; everything a *job* adds to a run — finding
    the account, decrypting its credential, storing the one the provider
    rotated mid-flight, reporting into the row the UI polls — is identical, so
    the mode is bound here and nothing else differs. A second handler would be
    the same twenty lines with one argument changed, and two places to forget
    the credential in. ``derive`` recomputes what the whole archive means, and
    ``embed`` computes the vectors the import deliberately never writes.

    An unmapped kind is a failure a *user* meets, not a developer: every "then
    run the embed job" sentence in this application — the no-embedder error,
    the coverage notice, the insights panel, the MCP tool docstrings — points
    at a row in this mapping. ``tests/test_worker.py`` therefore asserts
    against ``JobKind`` itself rather than against a written-out set, so the
    next kind that arrives without a handler fails a test instead of a job.
    """
    return {
        JobKind.IMPORT: partial(
            _import, engine, registry, session_factory, mode=SyncCursorKind.FULL
        ),
        JobKind.INCREMENTAL: partial(
            _import, engine, registry, session_factory, mode=SyncCursorKind.INCREMENTAL
        ),
        JobKind.DERIVE: partial(_derive, rebuild),
        JobKind.EMBED: partial(_embed, embed),
    }


async def run_worker() -> None:
    """Build everything a worker needs and run its two loops until they stop.

    Two loops, and only one of them is in charge. ``worker.run()`` owns the
    signal handlers and is what ends this process; the schedule is a strict
    subordinate, started before it and put down in the ``finally``, whatever
    ended the loop. An :class:`asyncio.TaskGroup` would invert that — it waits
    for *every* task it holds, so a schedule sitting out a fifteen-minute
    interval would keep the process alive a quarter of an hour past its
    SIGTERM.

    Nothing else changes about shutdown: the signal handler sets the worker's
    flag, its loop returns between jobs, and the job that was in flight keeps
    its lease until it expires and is reclaimed — the same path a ``kill -9``
    takes.

    The embedder settings a human stored are read once, here, before any job
    can be claimed: this process has its own composition root, and without the
    read it would embed with the configuration file while the web application
    searched with the stored row. Once rather than per job, because a worker is
    a child of the application that started it and goes down with it — a
    settings change reaches this loop at its next start. A failure to read them
    is logged and swallowed inside
    :func:`app.composition.adopt_semantic_settings`, for its stated reason: an
    un-migrated database must not cost the archive its import worker.
    """
    await adopt_semantic_settings()
    config = sync_config()
    queue = JobQueue()
    worker = JobWorker(
        queue,
        build_handlers(build_engine(), provider_registry()),
        worker_id=config.worker_id,
        lease_seconds=config.lease_seconds,
        heartbeat_seconds=config.heartbeat_interval,
        poll_seconds=config.poll_interval,
    )
    schedule = IntervalScheduler(
        queue, provider_registry(), interval_seconds=config.incremental_interval
    )
    sweeping = asyncio.create_task(schedule.run())
    try:
        await worker.run()
    finally:
        schedule.request_stop()
        await _stopped(sweeping)


async def _stopped(task: asyncio.Task[None]) -> None:
    """Wait for a task that has been asked to stop, and insist if it will not.

    The request is the polite half and normally the whole of it: a schedule
    parked on its interval waits on the same flag and returns at once. The
    grace and the cancel are for the other case — a sweep wedged on a database
    that has gone away must not hold up a process that has been told to exit,
    and abandoning one costs nothing, because a sweep only ever enqueues and
    each enqueue is its own committed transaction.

    Whatever the task kept for us is logged rather than raised: this runs in a
    ``finally``, and letting it out would replace the reason the worker
    actually stopped with a footnote about the schedule.
    """
    done, _ = await asyncio.wait({task}, timeout=STOP_GRACE_SECONDS)
    if not done:
        logger.warning(
            "The incremental schedule did not stop within %.0fs; cancelling it",
            STOP_GRACE_SECONDS,
        )
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # Only the cancellation this function asked for. A bare `pass` cannot
        # tell that one from "somebody is cancelling *us* while we wait here",
        # and swallowing the second would have `run_worker` return normally
        # from a `finally` — leaving the supervisor waiting for a signal that
        # never comes. `cancelling()` is non-zero exactly when this task is the
        # one being torn down.
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:
        logger.exception("The incremental schedule ended badly")


def main() -> None:
    """``python -m app.worker``: configure this process, then run the loop."""
    logging.basicConfig(level=logging.INFO)
    # Importing `app` already configured the process; saying so here keeps the
    # worker's dependency on configuration a statement rather than a side
    # effect. `configure` is cached, so this is free.
    configure()
    asyncio.run(run_worker())


async def _import(
    engine: ImportEngine,
    registry: ProviderRegistry,
    session_factory: SessionFactory,
    job: SyncJob,
    queue: JobQueue,
    *,
    mode: SyncCursorKind,
) -> None:
    """Walk one account's mailbox in *mode*, reporting into the row the UI reads.

    ``mode`` is keyword-only while its three neighbours are not, and the reason
    is :func:`functools.partial`: it prepends what it was given, so a bound
    positional has to sit to the *left* of the ``(job, queue)`` the loop calls
    with — and a fourth one there would have put six positional parameters in
    front of a reader who is trying to work out which two the loop supplies. A
    keyword binds just as well from the same ``partial`` and names itself at
    both ends, which is also how :meth:`ImportEngine.run` takes it.

    Errors are not caught here on purpose: the taxonomy of §7.6 is the loop's
    to act on, and swallowing an auth failure would cost a mailbox its
    re-consent prompt. That is also why the rebuild below is queued *after* the
    ``finally`` rather than inside it — a run that raised has left half a
    mailbox in the graph, and recomputing what the archive means over half a
    mailbox is worse than not recomputing it.
    """
    async with session_factory() as session:
        source, target, opened_with = await _open_mailbox(registry, session, job, mode)
    try:
        result = await engine.run(
            source,
            target,
            mode=mode,
            on_progress=partial(_report, queue, job.id),
            cancelled=partial(queue.is_cancel_requested, job.id),
        )
    finally:
        await _keep_refreshed_secret(
            session_factory, target.account_id, source, opened_with
        )
        await source.aclose()
    try:
        await _rebuild_after(queue, job, mode, result)
    except Exception:
        # Insured against the same way `IntervalScheduler._sweep` insures its
        # loop against a tick. By now the mail is archived and the watermark
        # has moved; a database that goes away while the follow-up is queued
        # must not turn that into a row saying the sync failed, with
        # `max_attempts=1` and no retry. A rebuild that was not queued costs
        # one button press.
        logger.exception("Could not queue the rebuild after job %d", job.id)


async def _rebuild_after(
    queue: JobQueue, job: SyncJob, mode: SyncCursorKind, result: ImportResult
) -> None:
    """Queue a rebuild of the derived layer when a scheduled delta brought mail in.

    **This queues the ordinary full rebuild; it is not an incremental
    recomputation, and phase 7's DoD asks for one.** Stated plainly rather than
    left to be inferred from a job named ``derive``: the rebuild deletes the
    derived layer before writing it, which makes running it again idempotent
    and running it often merely expensive. A genuinely incremental derive is a
    larger piece of work than this phase holds, because none of the three
    analyses is local to the new messages — a co-recipient group, a topic and a
    template are each a statement about the whole archive, and one new mail can
    move any of them.

    Only after a delta, and only when it archived something. A full import is
    something a human started and is watching, on a page that offers the
    rebuild button next to it; a delta at three in the morning has nobody to
    press it. A delta that archived nothing changed nothing, and rebuilding the
    whole derived layer over no new mail every interval is how a laptop
    discovers its fan.

    Not after a cancelled run either, even one that did archive: a human who
    pressed stop asked for work to end, not for an hour of graph writes to
    begin. The *row's* flag is read as well as the run's own view of itself,
    for the reason below the ``if``.

    Skipped while a rebuild is already open, for :meth:`JobQueue.find_open`'s
    reason — two rebuilds interleaving delete each other's rows.
    """
    if mode is not SyncCursorKind.INCREMENTAL:
        return
    if result.counts.archived == 0:
        return
    if result.cancelled or await queue.is_cancel_requested(job.id):
        # The row is asked as well as the run, because the run can miss it. A
        # delta is usually a single page, and the page loop breaks on "no next
        # cursor" *before* it asks whether to stop — so a stop pressed during
        # the only page produces a run that reports `cancelled=False` and a job
        # the queue then ends as cancelled. Reading only the run would answer
        # that press with an hour of graph writes.
        logger.info("Job %d was asked to stop; not queuing a rebuild", job.id)
        return
    if await queue.find_open(JobKind.DERIVE) is not None:
        logger.info("Job %d: a rebuild is already queued; not queuing a second", job.id)
        return
    rebuild_id = await queue.enqueue(JobKind.DERIVE)
    logger.info(
        "Job %d archived %d new message(s); queued rebuild %d",
        job.id,
        result.counts.archived,
        rebuild_id,
    )


async def _keep_refreshed_secret(
    session_factory: SessionFactory,
    account_id: int,
    source: MailSourcePort,
    opened_with: str,
) -> None:
    """Store a credential the provider rotated while the run was going.

    Google reissues a refresh token on a re-consent and around the idle-expiry
    path, and the new one arrives silently in the middle of an import. Nothing
    re-reads it afterwards, so without this the run finishes fine and the
    *next* unattended one authenticates with a token that has been superseded
    — an ``auth_error`` with no hint that a working credential was handed to us
    and dropped.

    Duck-typed rather than reached through a ``Protocol``: Gmail is the only
    provider whose credentials rotate, and §3.1 is explicit that a Protocol
    earns its place when a second implementation exists. A provider that has
    nothing to say here says nothing.

    Never allowed to fail the job: the mail is already archived by the time
    this runs, and losing a rotated token costs one re-consent while raising
    here would cost the whole import. That broad catch is why the write goes
    through :meth:`MailCredentialRepository.store_secret` rather than assigning
    ``secret`` here — a failed write raises ``CredentialNotStored``, which says
    why without quoting the token the ``logger.exception`` below would
    otherwise print.
    """
    to_secret = getattr(getattr(source, "credentials", None), "to_secret", None)
    if to_secret is None:
        return
    try:
        current = to_secret()
        if current == opened_with:
            return
        async with session_factory() as session:
            for kind in CredentialKind:
                credential = await _CREDENTIALS.find_by_account(
                    session, account_id, kind
                )
                if credential is not None and credential.secret == opened_with:
                    await _CREDENTIALS.store_secret(
                        session,
                        account_id=account_id,
                        kind=kind,
                        secret=current,
                    )
        logger.info("Stored the credential account %d refreshed mid-run", account_id)
    except Exception:
        logger.exception(
            "Could not store the refreshed credential of account %d", account_id
        )


async def _open_mailbox(
    registry: ProviderRegistry,
    session: AsyncSession,
    job: SyncJob,
    mode: SyncCursorKind,
) -> tuple[MailSourcePort, ImportTarget, str]:
    """The source, the target and the secret it was opened with.

    All three inside the caller's session on purpose: a factory reads what it
    needs off the account row, and a row whose session has closed hands back
    nothing. The secret comes back too so the run can tell afterwards whether
    the provider rotated it — see :func:`_keep_refreshed_secret`.

    ``mode`` is only logged. Opening a mailbox is the same act either way — the
    difference between a walk and a delta is a cursor, and the cursor is the
    engine's — but a log line that says which of the two is starting is the
    difference between reading a scheduled run's trace and guessing at it.
    """
    if job.account_id is None:
        raise LookupError(f"job {job.id} reads a mailbox, but names no account")
    account = await _ACCOUNTS.find_by_id(session, job.account_id)
    if account is None:
        raise LookupError(f"job {job.id} names account {job.account_id}, which is gone")

    provider = MailProvider(account.provider)
    secret = await _secret_for(session, account.id)
    source = registry.factory_for(provider)(account, secret)
    target = ImportTarget(
        account_id=account.id, address=account.email_address, provider=provider
    )
    logger.info(
        "Job %d runs a %s sync of %s (%s)",
        job.id,
        mode.value,
        account.email_address,
        provider,
    )
    return source, target, secret


async def _secret_for(session: AsyncSession, account_id: int) -> str:
    """The account's stored secret, whichever kind it turns out to be.

    Which kind an account has is the provider's business — OAuth for Gmail, a
    password for IMAP — and there is at most one per kind, so the first one
    found is the one that opens the mailbox. Having none is not a mailbox
    fault but an unfinished setup, and :class:`MailAuthError` is what puts that
    in front of a human instead of retrying it.
    """
    for kind in CredentialKind:
        credential = await _CREDENTIALS.find_by_account(session, account_id, kind)
        if credential is not None:
            return credential.secret
    raise MailAuthError(f"account {account_id} has no stored credential")


async def _report(queue: JobQueue, job_id: int, progress: ImportProgress) -> None:
    """Copy a page's tally into the job row.

    A message that was already ours counts as done rather than as nothing: the
    number a progress bar needs is how much of the mailbox has been dealt with.
    """
    counts = progress.counts
    await queue.progress(
        job_id,
        done=counts.archived + counts.skipped,
        failed=counts.failed,
        total=progress.estimated_total,
    )


async def _derive(rebuild: DerivedRebuild, job: SyncJob, queue: JobQueue) -> None:
    """Recompute what the archive means, reporting into the row the UI reads.

    Off the event loop, because every runic driver blocks and this one holds a
    session for the length of a full read of the archive — the same reason the
    import engine parses and writes in threads. The loop stays free to answer
    the pages that are watching this job.

    A derive job names no account (``account_id`` is ``None``): it is about the
    whole archive, and nothing on this path asks which mailbox it came from.

    Errors are not caught here beyond the cancel: §7.6's taxonomy belongs to
    the loop, and a rebuild that failed halfway has to end as a failed job so
    that what is in the graph is known to be half a layer.
    """
    loop = asyncio.get_running_loop()
    logger.info("Job %d rebuilds the derived layer", job.id)
    try:
        counts = await asyncio.to_thread(
            rebuild, partial(_stage_done, loop, queue, job.id)
        )
    except _Stopped:
        logger.info("Job %d stopped between stages after a cancel", job.id)
        return
    logger.info(
        "Job %d wrote %d groups, %d topics and %d templates",
        job.id,
        counts.groups,
        counts.topics,
        counts.templates,
    )


async def _embed(embed: MessageEmbedding, job: SyncJob, queue: JobQueue) -> None:
    """Fill in the vectors, reporting into the row the UI reads.

    On the loop, unlike :func:`_derive`, and the difference is not an
    oversight: a rebuild is blocking graph work from end to end, while an embed
    run is mostly waiting on somebody else's HTTP server and already hands each
    graph call to a thread of its own. Wrapping this in ``asyncio.to_thread``
    would put an event loop's worth of awaiting inside a worker thread for no
    gain.

    An embed job names no account (``account_id`` is ``None``): it is about the
    whole archive, and nothing on this path asks which mailbox a message came
    from.

    Nothing is caught. ``SemanticUnavailable`` carries the sentence naming the
    setting to change and belongs in the row's error column word for word;
    §7.6's taxonomy belongs to the loop, which is what turns a model server
    that is still warming up into a retry with backoff rather than into a
    failed archive.
    """
    logger.info("Job %d embeds the messages that have no vector yet", job.id)
    run = await embed(
        partial(_vectors_done, queue, job.id),
        partial(queue.is_cancel_requested, job.id),
    )
    logger.info(
        "Job %d embedded %d of %d messages (%d refused%s)",
        job.id,
        run.done,
        run.total,
        run.failed,
        ", stopped on request" if run.cancelled else "",
    )


async def _vectors_done(queue: JobQueue, job_id: int, run: EmbedRun) -> None:
    """Copy one batch's tally into the job row.

    Messages rather than stages, unlike :func:`_stage_done`: an embed run knows
    up front how many messages it owes a vector, so ``done`` out of ``total``
    is the number a progress bar is actually asking for. ``done`` counts nodes
    written and not messages attempted, which is what makes it match what a
    search can find.
    """
    await queue.progress(job_id, done=run.done, failed=run.failed, total=run.total)


def _stage_done(
    loop: asyncio.AbstractEventLoop,
    queue: JobQueue,
    job_id: int,
    progress: RebuildProgress,
) -> None:
    """Move the job row on by one stage, then ask whether to carry on.

    Called from the rebuild's thread, once per stage, and both halves of it go
    back to *the handler's* loop: the queue reaches ``mail_sync_jobs`` through
    an async session, and a thread that opened its own would be a second writer
    on the row this job owns.

    Asking after reporting rather than before, so a cancel that arrives during
    a stage still leaves the row showing that the stage finished. What the
    stage actually produced goes to the log, where a number that means
    something different every stage does no harm.
    """
    logger.info(
        "Job %d finished the %s stage: %d of %d",
        job_id,
        progress.stage,
        progress.done,
        progress.total,
    )
    _awaited(
        loop,
        queue.progress(
            job_id,
            done=_STAGES.index(progress.stage) + 1,
            failed=0,
            total=len(_STAGES),
        ),
    )
    if _awaited(loop, queue.is_cancel_requested(job_id)):
        raise _Stopped(f"job {job_id} was cancelled during {progress.stage}")


def _awaited[T](loop: asyncio.AbstractEventLoop, work: Coroutine[Any, Any, T]) -> T:
    """Run one coroutine on *loop* from another thread and wait for its answer.

    Waiting here cannot deadlock as long as this is only ever called out of the
    rebuild thread: the loop is parked on the ``asyncio.to_thread`` that
    started that thread, so it is free to take the coroutine straight away.
    """
    return asyncio.run_coroutine_threadsafe(work, loop).result()


if __name__ == "__main__":
    main()
