"""The job table, driven as a state machine.

::

    queued ──claim──> running ──> succeeded
                         │└──────> failed      (with error)
                         └───────> cancelled   (cancel_requested)

The claim is a conditional ``UPDATE`` whose ``WHERE`` names the state we expect
to find; ``rowcount`` decides who won. SQLite has no ``SKIP LOCKED`` and does
not need one — a compare-and-swap lets exactly one worker through, and the
loser tries the next candidate. WAL, ``busy_timeout`` and ``foreign_keys`` come
from :func:`mailarc_core.database.sqlite.install_pragmas` and hold in the app
process and the worker process alike.

Every method opens its own session and commits it. That is the point: a lease
that exists only inside our transaction protects nothing from the other
process.
"""

import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from appkit_commons.database.session import get_asyncdb_session
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mailarc_core.database.entities import MailSyncJobEntity, SyncJobState
from mailarc_core.database.repositories import SyncJobRepository
from mailarc_sync.jobs.model import JobKind, JobProgress, JobState, SyncJob

logger = logging.getLogger(__name__)

type SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
"""Opens a session and owns its transaction: commit on a clean exit, else roll back.

``get_asyncdb_session`` is the one the application passes; a test passes its
own so it can point at a file it owns instead of the configured database.
"""

_OPEN_STATES = (SyncJobState.QUEUED, SyncJobState.RUNNING)
"""The states a job can still be asked to stop from."""


class JobQueue:
    """Enqueue, claim, report, finish — and nothing beyond the diagram above.

    Deliberately not a scheduler: there is no trigger, no cron and no calendar
    here. Something else decides *when* a job should exist; this decides who
    gets to run it and what happened.
    """

    def __init__(self, session_factory: SessionFactory = get_asyncdb_session) -> None:
        self._session_factory = session_factory
        self._jobs = SyncJobRepository()

    async def enqueue(self, kind: JobKind, account_id: int | None = None) -> int:
        """Put a job in the queue and return its id."""
        async with self._session_factory() as session:
            job = await self._jobs.create(
                session,
                MailSyncJobEntity(
                    kind=kind,
                    account_id=account_id,
                    state=SyncJobState.QUEUED,
                ),
            )
            job_id = job.id
        logger.info("Queued %s job %d for account %s", kind, job_id, account_id)
        return job_id

    async def get(self, job_id: int) -> SyncJob | None:
        """Read a job back — the only way anything outside sees its counters."""
        async with self._session_factory() as session:
            entity = await self._jobs.find_by_id(session, job_id)
            return None if entity is None else _as_job(entity)

    async def find_open(
        self, kind: JobKind, account_id: int | None = None
    ) -> SyncJob | None:
        """The oldest job of *kind* that has not ended yet, or ``None``.

        The question a panel has to ask before it queues anything: "is one of
        these already going?". Without it a page can only know about a job it
        started itself, so two open tabs — or one tab after a reload, which
        forgets ``job_id`` — each queue their own. For a ``derive`` that is not
        merely wasted work: a rebuild starts by deleting the derived layer, so
        two of them interleaving can wipe rows the other has already written.

        ``account_id`` is matched exactly, ``None`` included, because ``None``
        is a real value here and not a wildcard — a ``derive`` is about the
        whole archive and carries no account, which is precisely the row this
        has to find.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(MailSyncJobEntity)
                .where(
                    MailSyncJobEntity.kind == kind,
                    MailSyncJobEntity.account_id.is_(None)
                    if account_id is None
                    else MailSyncJobEntity.account_id == account_id,
                    MailSyncJobEntity.state.in_(_OPEN_STATES),
                )
                .order_by(MailSyncJobEntity.id)
                .limit(1)
            )
            entity = result.scalar_one_or_none()
            return None if entity is None else _as_job(entity)

    async def claim(self, worker_id: str, lease_seconds: float) -> SyncJob | None:
        """Take the oldest queued job, or return ``None`` if there is none.

        The lease is the whole ownership story: while it is in the future the
        job is ours, and when it stops moving the job is up for grabs again.
        """
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            for job_id in await self._queued_ids(session):
                if not await self._take(session, job_id, worker_id, lease_seconds, now):
                    logger.debug("Job %d went to another worker", job_id)
                    continue
                entity = await session.get(MailSyncJobEntity, job_id)
                if entity is None:  # pragma: no cover - deleted mid-claim
                    continue
                logger.info("Worker %s claimed job %d", worker_id, job_id)
                return _as_job(entity)
        return None

    async def heartbeat(self, job_id: int, lease_seconds: float) -> bool:
        """Push the lease out; ``False`` means the job is no longer running.

        A worker told ``False`` has lost the job — its lease ran out and
        someone else took it over, or the job was ended underneath it — and
        must stop rather than keep writing.
        """
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            result = await self._write(
                session,
                MailSyncJobEntity.id == job_id,
                MailSyncJobEntity.state == SyncJobState.RUNNING,
                lease_until=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
            )
        return result == 1

    async def reclaim_expired(self, *, now: datetime | None = None) -> int:
        """Put every job whose lease ran out back in the queue; return how many.

        This is the entire crash story. A worker that dies stops heartbeating,
        its lease expires, and the next worker takes the job over and resumes
        from the checkpoint the dead one wrote. No lock server, no liveness
        protocol — a timestamp that stopped moving says everything.
        """
        deadline = now or datetime.now(UTC)
        async with self._session_factory() as session:
            reclaimed = await self._write(
                session,
                MailSyncJobEntity.state == SyncJobState.RUNNING,
                MailSyncJobEntity.lease_until.is_not(None),
                MailSyncJobEntity.lease_until < deadline,
                state=SyncJobState.QUEUED,
                worker_id=None,
                lease_until=None,
            )
        if reclaimed:
            logger.warning("Reclaimed %d job(s) whose lease had expired", reclaimed)
        return reclaimed

    async def progress(
        self, job_id: int, done: int, failed: int, total: int | None = None
    ) -> bool:
        """Record how far the job got. ``total`` left out keeps the estimate.

        Guarded on ``running`` like every other write in here. A worker whose
        lease expired mid-page is still holding counters, and without the guard
        its last report would land on top of the numbers the worker that took
        the job over is now producing — a progress bar that walks backwards.
        ``False`` says the job had already moved on.
        """
        values: dict[str, Any] = {"messages_done": done, "messages_failed": failed}
        if total is not None:
            values["messages_total"] = total
        async with self._session_factory() as session:
            written = await self._write(
                session,
                MailSyncJobEntity.id == job_id,
                MailSyncJobEntity.state == SyncJobState.RUNNING,
                **values,
            )
        if not written:
            logger.debug("Job %d is not running; progress dropped", job_id)
            return False
        logger.debug("Job %d progress: %d done, %d failed", job_id, done, failed)
        return True

    async def succeed(self, job_id: int) -> None:
        """End the job: it did what it was queued for."""
        await self._finish(job_id, SyncJobState.SUCCEEDED, None)

    async def fail(self, job_id: int, error: str) -> None:
        """End the job with the reason a human will read in the UI."""
        await self._finish(job_id, SyncJobState.FAILED, error)

    async def cancel(self, job_id: int) -> None:
        """End the job because it was asked to stop and did."""
        await self._finish(job_id, SyncJobState.CANCELLED, None)

    async def request_cancel(self, job_id: int) -> bool:
        """Ask a job to stop; ``False`` if it had already ended.

        A flag, not a kill — *for a job somebody is running*. The worker reads
        it between batches, so whatever was half-written when a human clicked
        cancel is still written whole.

        A job still QUEUED is the other case, and it used to fall through to
        the same flag: nobody had claimed it, so nobody was going to read the
        flag, and with no worker up at all — the normal state of a dev machine
        — it stayed queued and *active* for ever. The panel above it then
        showed a rebuild that claimed to be running, two disabled buttons and
        no way out but a reload, which queued a second job. So an unclaimed job
        is ended here and now: the half-written stage the flag exists to
        protect does not exist yet.

        Two conditional writes rather than a read and a write. The first only
        matches while the job is still QUEUED, so a worker that claims it in
        between loses that write and wins the second — the job goes on running
        and gets the flag, which is the correct outcome for a job that now has
        an owner.
        """
        async with self._session_factory() as session:
            ended = await self._write(
                session,
                MailSyncJobEntity.id == job_id,
                MailSyncJobEntity.state == SyncJobState.QUEUED,
                state=SyncJobState.CANCELLED,
                cancel_requested=True,
                finished_at=datetime.now(UTC),
                worker_id=None,
                lease_until=None,
            )
            if ended == 1:
                logger.info("Job %d cancelled before any worker claimed it", job_id)
                return True
            asked = await self._write(
                session,
                MailSyncJobEntity.id == job_id,
                MailSyncJobEntity.state.in_(_OPEN_STATES),
                cancel_requested=True,
            )
        logger.info("Cancel requested for job %d: %s", job_id, bool(asked))
        return asked == 1

    async def is_cancel_requested(self, job_id: int) -> bool:
        """Read the flag. A handler asks between batches, never inside one."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(MailSyncJobEntity.cancel_requested).where(
                    MailSyncJobEntity.id == job_id
                )
            )
            return bool(result.scalar_one_or_none())

    async def _finish(
        self, job_id: int, state: SyncJobState, error: str | None
    ) -> None:
        """Write one of the three end states and let go of the lease.

        Unconditional on purpose: the caller holds the lease, so it is the one
        entitled to say how the job ended.
        """
        async with self._session_factory() as session:
            await self._write(
                session,
                MailSyncJobEntity.id == job_id,
                state=state,
                error=error,
                finished_at=datetime.now(UTC),
                worker_id=None,
                lease_until=None,
            )
        logger.info("Job %d ended as %s", job_id, state)

    async def _take(
        self,
        session: AsyncSession,
        job_id: int,
        worker_id: str,
        lease_seconds: float,
        now: datetime,
    ) -> bool:
        """The compare-and-swap itself: ``rowcount`` is the answer.

        ``started_at`` is only stamped once, so a job resumed after a crash
        still reports when its work actually began.
        """
        taken = await self._write(
            session,
            MailSyncJobEntity.id == job_id,
            MailSyncJobEntity.state == SyncJobState.QUEUED,
            state=SyncJobState.RUNNING,
            worker_id=worker_id,
            lease_until=now + timedelta(seconds=lease_seconds),
            heartbeat_at=now,
            started_at=func.coalesce(MailSyncJobEntity.started_at, now),
            error=None,
        )
        return taken == 1

    async def _queued_ids(self, session: AsyncSession) -> Sequence[int]:
        """Candidate ids, oldest first.

        Ids only, never entities: loading a job here would put it in the
        session's identity map, and the row we read back after the swap has to
        be the row the database has, not the one we saw before it.
        """
        result = await session.execute(
            select(MailSyncJobEntity.id)
            .where(MailSyncJobEntity.state == SyncJobState.QUEUED)
            .order_by(MailSyncJobEntity.id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def _write(session: AsyncSession, *where: Any, **values: Any) -> int:
        """Run one conditional ``UPDATE`` and return how many rows it matched.

        ``synchronize_session=False`` because nothing in this module reads a
        job through the ORM after writing it — the extra ``SELECT`` SQLAlchemy
        would issue to keep an identity map honest has nothing to keep honest.

        ``AsyncSession.execute`` is typed as returning a plain ``Result``; an
        ``UPDATE`` always yields a ``CursorResult``, which is the one that
        carries the row count the whole state machine turns on.
        """
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(MailSyncJobEntity)
                .where(*where)
                .values(**values)
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount


def _as_job(entity: MailSyncJobEntity) -> SyncJob:
    """The row, detached from its session."""
    return SyncJob(
        id=entity.id,
        kind=JobKind(entity.kind),
        state=JobState(entity.state),
        account_id=entity.account_id,
        worker_id=entity.worker_id,
        lease_until=entity.lease_until,
        heartbeat_at=entity.heartbeat_at,
        cancel_requested=entity.cancel_requested,
        progress=JobProgress(
            total=entity.messages_total,
            done=entity.messages_done,
            failed=entity.messages_failed,
        ),
        error=entity.error,
        started_at=entity.started_at,
        finished_at=entity.finished_at,
    )
