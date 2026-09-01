"""The poll loop: claim a job, run it, say what happened, repeat.

Nothing else lives here. No process management, no configuration building, no
knowledge of what an ``import`` job actually does — ``app/worker.py`` owns all
three, because that is the composition root and this is a library. The mapping
from job kind to handler is passed in for the same reason.

What the loop does own is the part every handler would otherwise repeat: the
lease stays alive while a handler works, a cancel that arrives mid-flight is
honoured once the handler comes up for air, and the error taxonomy of §7.6
decides whether a failure ends the job, waits, or leaves a row behind.
"""

import asyncio
import contextlib
import logging
import os
import random
import signal
import socket
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any

from appkit_commons.database.session import get_asyncdb_session

from mailarc_core.database.entities import AccountStatus
from mailarc_core.database.repositories import (
    FailedMessageRepository,
    MailAccountRepository,
)
from mailarc_core.mail.errors import (
    MailAuthError,
    MailPermanentError,
    MailTransientError,
)
from mailarc_sync.jobs.model import JobKind, SyncJob
from mailarc_sync.jobs.queue import JobQueue, SessionFactory

logger = logging.getLogger(__name__)

type JobHandler = Callable[[SyncJob, JobQueue], Awaitable[None]]
"""What the worker dispatches to.

A handler gets the job and the queue, and that is the whole contract: the queue
is how it reports progress and how it asks, between batches, whether it should
stop. Anything else it needs comes from a closure built in ``app/worker.py``.
A context object here would only move the wiring one layer down.
"""

DEFAULT_LEASE_SECONDS = 60.0
"""How long a claim holds without a heartbeat before another worker may take it."""

DEFAULT_HEARTBEAT_SECONDS = 10.0
"""§7.2: the worker pushes the lease out every ten seconds."""

DEFAULT_POLL_SECONDS = 2.0
"""How long an idle worker waits before asking for work again."""

DEFAULT_MAX_ATTEMPTS = 1
"""How often a handler is run before a transient failure is called failed.

One, because the handler is the layer that knows what to retry. The import
engine already retries a *slice* five times with backoff before letting
:class:`~mailarc_core.mail.errors.MailTransientError` out, so a second budget
here would multiply into twenty-five attempts and four extra walks of the whole
mailbox — for an error that by then means an outage, not a hiccup.

Raise it for a handler that does no retrying of its own; the loop still knows
how to back off.
"""

DEFAULT_BACKOFF_SECONDS = 1.0
"""The first wait after a transient failure; it doubles from there."""

DEFAULT_MAX_BACKOFF_SECONDS = 60.0
"""The ceiling the doubling runs into."""

JITTER_RATIO = 0.25
"""Spread added on top of a backoff, so jobs that failed together do not return together."""

WORKER_ID_LENGTH = 64
"""The width of ``mail_sync_jobs.worker_id``."""

UNKNOWN_MESSAGE_ID = "unknown"
"""Stands in when a permanent failure reached the worker without naming a message."""

PERMANENT_REASON = "permanent"
"""The §7.6 taxonomy's short name, as written to ``mail_failed_messages.reason``."""


def default_worker_id() -> str:
    """Name this worker, so a lease says who is holding it.

    Process id first: if a very long hostname pushes against the column width,
    the half that tells two workers apart is the half that survives.
    """
    return f"{os.getpid()}@{socket.gethostname()}"[:WORKER_ID_LENGTH]


class JobWorker:
    """Runs queued jobs until it is asked to stop.

    One job at a time and one worker per process. Serialising the archive
    writer is cheaper than coordinating several (§7.3), and a queue that hands
    out one job at a time needs no fairness rules.
    """

    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[JobKind, JobHandler],
        *,
        worker_id: str | None = None,
        session_factory: SessionFactory = get_asyncdb_session,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        handle_signals: bool = True,
    ) -> None:
        self._queue = queue
        self._handlers = handlers
        self._worker_id = worker_id or default_worker_id()
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_seconds = poll_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._handle_signals = handle_signals
        self._stopping = asyncio.Event()
        self._accounts = MailAccountRepository()
        self._failures = FailedMessageRepository()

    @property
    def worker_id(self) -> str:
        """The name this worker writes into the lease."""
        return self._worker_id

    async def run(self) -> None:
        """Claim and run jobs until asked to stop.

        Returns on a stop request and re-raises :class:`asyncio.CancelledError`
        after putting the in-flight job down, so a supervisor that cancels this
        task still gets its cancellation.
        """
        logger.info("Worker %s started", self._worker_id)
        try:
            with self._signals():
                await self._loop()
        finally:
            logger.info("Worker %s stopped", self._worker_id)

    def request_stop(self) -> None:
        """Ask the loop to finish.

        Safe to call from a signal handler: it sets a flag and returns. The
        in-flight job is put down rather than waited out — a full import can
        run for hours, and its checkpoint is what a restart resumes from.
        """
        if not self._stopping.is_set():
            logger.info("Worker %s asked to stop", self._worker_id)
        self._stopping.set()

    async def _loop(self) -> None:
        """Sweep, claim, run. The sweep first, so a dead worker's job comes back."""
        while not self._stopping.is_set():
            await self._queue.reclaim_expired()
            job = await self._queue.claim(self._worker_id, self._lease_seconds)
            if job is None:
                await self._idle()
                continue
            await self._run_job(job)

    async def _idle(self) -> None:
        """Wait for the next poll or for the stop request, whichever lands first."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), self._poll_seconds)

    async def _run_job(self, job: SyncJob) -> None:
        """Run one claimed job with its lease kept alive around it.

        A stop request abandons the job rather than ending it: the lease runs
        out, :meth:`JobQueue.reclaim_expired` puts it back, and the next start
        resumes at the checkpoint. That is the same path a ``kill -9`` takes,
        and one path is worth more than two.

        Losing the lease takes that same path. The heartbeat is waited on
        alongside the work for exactly that reason: it only ever finishes when
        the job has stopped being ours, and a handler that keeps running past
        that point would import a mailbox a second worker is already importing
        and then write an outcome onto that worker's job.
        """
        handler = self._handlers.get(job.kind)
        if handler is None:
            logger.error(
                "No handler registered for kind %s; job %d fails", job.kind, job.id
            )
            await self._queue.fail(job.id, f"no handler registered for kind {job.kind}")
            return

        work = asyncio.create_task(self._attempt(job, handler))
        beat = asyncio.create_task(self._heartbeat(job.id))
        stop = asyncio.create_task(self._stopping.wait())
        try:
            done, _ = await asyncio.wait(
                {work, beat, stop}, return_when=asyncio.FIRST_COMPLETED
            )
            if work in done:
                await work
            elif beat in done:
                logger.warning("Job %d is not ours any more; letting go of it", job.id)
            else:
                logger.info("Stopping; job %d keeps its lease until it expires", job.id)
        finally:
            for task in (work, beat, stop):
                await _cancel(task)

    async def _heartbeat(self, job_id: int) -> None:
        """Keep the lease alive while the handler works.

        Returns the moment the queue says the job is not ours any more — its
        lease ran out and someone else claimed it, or it was ended underneath
        us. :meth:`_run_job` waits on this task, so returning is how the work
        gets called off; a worker that lost a job must neither push out the
        lease of the one that took over nor keep writing to the archive on its
        behalf.

        Returning is therefore reserved for a *refusal*. A database hiccup
        while extending the lease is logged and tried again on the next beat:
        the lease is a safety net, not the work, and an error here must not
        replace the outcome the handler has earned.
        """
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                held = await self._queue.heartbeat(job_id, self._lease_seconds)
            except Exception:
                logger.exception("Could not extend the lease of job %d", job_id)
                continue
            if not held:
                logger.warning("Job %d is no longer ours; heartbeat stops", job_id)
                return

    async def _attempt(self, job: SyncJob, handler: JobHandler) -> None:
        """Run the handler and turn whatever it raises into a job outcome."""
        for attempt in range(self._max_attempts):
            try:
                await handler(job, self._queue)
            except MailAuthError as exc:
                await self._mark_auth_error(job, exc)
                await self._queue.fail(job.id, f"auth: {exc}")
                return
            except MailTransientError as exc:
                if attempt + 1 >= self._max_attempts:
                    logger.error(
                        "Job %d gave up after %d attempts", job.id, attempt + 1
                    )
                    await self._queue.fail(job.id, f"transient: {exc}")
                    return
                delay = self._backoff(attempt, exc.retry_after)
                logger.warning(
                    "Job %d hit a transient failure, retrying in %.1fs: %s",
                    job.id,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
            except MailPermanentError as exc:
                await self._record_permanent(job, exc)
                await self._queue.fail(job.id, f"permanent: {exc}")
                return
            except Exception as exc:
                logger.exception("Job %d failed unexpectedly", job.id)
                await self._queue.fail(job.id, f"{type(exc).__name__}: {exc}")
                return
            else:
                await self._settle(job)
                return

    async def _settle(self, job: SyncJob) -> None:
        """End a handler that returned: cancelled if a human asked for it.

        The flag decides, not the handler. A handler stops by returning between
        batches, and the queue is the only place that knows whether that was
        the end of the work or the answer to a cancel.
        """
        if await self._queue.is_cancel_requested(job.id):
            logger.info("Job %d stopped between batches after a cancel", job.id)
            await self._queue.cancel(job.id)
            return
        await self._queue.succeed(job.id)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Exponential, capped, and never below the provider's own ``Retry-After``.

        ``Retry-After`` is a floor rather than the answer: the jitter on top is
        what keeps a hundred jobs that failed together from returning together.
        """
        delay = min(self._backoff_seconds * 2**attempt, self._max_backoff_seconds)
        delay = max(delay, retry_after or 0.0)
        return delay + delay * JITTER_RATIO * random.random()  # noqa: S311 - spread, not a secret

    async def _mark_auth_error(self, job: SyncJob, exc: MailAuthError) -> None:
        """Put the account in ``auth_error`` so the UI offers a re-consent.

        Retrying a revoked token until the quota runs out is the failure mode
        this exists to prevent.
        """
        if job.account_id is None:
            return
        async with self._session_factory() as session:
            account = await self._accounts.find_by_id(session, job.account_id)
            if account is None:
                logger.warning(
                    "Job %d names account %d, which is gone", job.id, job.account_id
                )
                return
            account.status = AccountStatus.AUTH_ERROR
            account.last_error = str(exc)
        logger.error("Account %d needs re-consent: %s", job.account_id, exc)

    async def _record_permanent(self, job: SyncJob, exc: MailPermanentError) -> None:
        """Leave the row a skipped message owes us.

        The engine skips a broken message and carries on, recording it as it
        goes. One that gets this far took the job down with it and we no longer
        know which message it was — but the row is still written, because a
        drop nobody can count is the one thing §7.6 forbids.
        """
        if job.account_id is None:
            return
        async with self._session_factory() as session:
            await self._failures.record(
                session,
                account_id=job.account_id,
                provider_message_id=UNKNOWN_MESSAGE_ID,
                reason=PERMANENT_REASON,
                detail=str(exc),
            )

    @contextlib.contextmanager
    def _signals(self) -> Iterator[None]:
        """Wire SIGTERM and SIGINT to a stop request, and unwire them on the way out.

        A signal must not end the process mid-write. It asks the loop to stop;
        the in-flight job is put down and reclaimed at its lease. Installing a
        handler fails outside the main thread and on platforms without one, and
        a worker that cannot hear SIGTERM is still a worker.
        """
        if not self._handle_signals:
            yield
            return
        loop = asyncio.get_running_loop()
        wired: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError, RuntimeError, ValueError:
                logger.debug("No handler installed for %s here", sig.name)
                continue
            wired.append(sig)
        try:
            yield
        finally:
            for sig in wired:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)


async def _cancel(task: asyncio.Task[Any]) -> None:
    """Cancel a task and wait for it, so nothing keeps running behind us.

    Runs while the job's outcome is already decided, so an exception the task
    kept for us is logged rather than raised: letting it out here would replace
    that outcome and skip the tasks still waiting to be put down.
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("A background task of the worker ended badly")
