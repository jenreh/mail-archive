"""What a job is, before anyone stores or runs one.

Frozen value objects and no I/O. :class:`SyncJob` is the row once it has left
its session: the worker hands it to a handler and the UI renders it, and
neither should have to keep a database session open to read a counter.

``JobKind`` and ``JobState`` are the job table's own vocabulary, imported under
the names this package uses rather than restated. The queue's compare-and-swap
compares these strings against what is in the column, so a second copy of them
would be a second truth — and the one that drifts would drift silently.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from mailarc_core.database.entities import SyncJobKind as JobKind
from mailarc_core.database.entities import SyncJobState as JobState

__all__ = ["JobKind", "JobProgress", "JobState", "SyncJob"]


class JobProgress(BaseModel):
    """How far a job got, counted in messages.

    ``done`` and ``failed`` are disjoint — a message is archived or it left a
    row in ``mail_failed_messages``, never both. ``total`` is the provider's
    estimate and may still grow while the job runs, so a progress bar built on
    it has to survive being told the finish line moved.
    """

    model_config = ConfigDict(frozen=True)

    total: int = 0
    done: int = 0
    failed: int = 0


class SyncJob(BaseModel):
    """One unit of work, as everything outside the queue sees it.

    A snapshot, not a handle: the values were true when the queue read them.
    Only the queue may act on a job, and it acts by id, so a stale copy in a
    UI can be wrong without being dangerous.

    ``account_id`` is ``None`` for the kinds that work on the whole archive
    instead of one mailbox — ``derive`` and ``embed``.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    kind: JobKind
    state: JobState
    account_id: int | None = None
    worker_id: str | None = None
    lease_until: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested: bool = False
    progress: JobProgress = Field(default_factory=JobProgress)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
