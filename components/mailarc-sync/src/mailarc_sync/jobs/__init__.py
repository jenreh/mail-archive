"""Work that outlives the process that started it.

An import runs for hours and a laptop lid closes. So progress is a row rather
than a stack frame: a job is claimed under a lease, reports what it got done,
and — if the worker dies — is taken over by the next one at the checkpoint the
dead one wrote. Nothing here knows what a job *does*; the handler does, and it
is wired in at the composition root.

The appkit scheduler was measured against this and does not fit (§3.3): it is a
cron with a trigger, not a work queue with a lease, and it silently falls back
to an in-memory scheduler on anything but PostgreSQL. Hence a table of our own.

One module per concern:

``model``
    ``JobKind``, ``JobState``, ``JobProgress``, ``SyncJob``. No I/O.
``queue``
    ``JobQueue`` — the state machine over ``mail_sync_jobs``.
``worker``
    ``JobWorker`` — the poll loop, and nothing about processes or config.
"""

from mailarc_sync.jobs.model import JobKind, JobProgress, JobState, SyncJob
from mailarc_sync.jobs.queue import JobQueue, SessionFactory
from mailarc_sync.jobs.worker import JobHandler, JobWorker, default_worker_id

__all__ = [
    "JobHandler",
    "JobKind",
    "JobProgress",
    "JobQueue",
    "JobState",
    "JobWorker",
    "SessionFactory",
    "SyncJob",
    "default_worker_id",
]
