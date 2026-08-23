"""How hard the import pushes, and how long a worker may go quiet.

Two groups of settings live here. The first four are the pipeline's shape —
how much is listed at once, how much is fetched in parallel, how often the
run writes down where it got to. The rest belong to the worker loop: how often
it looks for work, how long a claim survives without a sign of life, how often
it goes looking for new mail on its own, and whether the application is the one
that starts it at all.

Everything else about an import — which mailbox, which folder, when — is state
in SQLite, not configuration.
"""

import os
import socket

from appkit_commons.configuration.base import BaseConfig
from pydantic import Field
from pydantic_settings import SettingsConfigDict

WORKER_ID_LENGTH = 64
"""The width of ``mail_sync_jobs.worker_id``; a longer id would not fit."""


def default_worker_id() -> str:
    """Name this process on a machine, so a lease says who holds it.

    Pid and host, because that is what a human needs when a job has been
    ``running`` for an hour: what to kill and where. Pid first so that a very
    long hostname loses the half that does *not* tell two workers apart.
    """
    return f"{os.getpid()}@{socket.gethostname()}"[:WORKER_ID_LENGTH]


class SyncConfig(BaseConfig):
    """Settings for the import engine and the worker that drives it."""

    model_config = SettingsConfigDict(
        env_prefix="app_sync_",
        env_file=".env",
        populate_by_name=True,
    )

    batch_size: int = 100
    """Message references the engine lists in one page.

    Also the archive stage's write batch, so one page is one graph session and
    one relational transaction.
    """

    fetch_concurrency: int = 8
    """How many fetch streams may be open at once — the semaphore of §7.3.

    The limit is on the provider's patience, not on ours: eight concurrent
    conversations keep a first import moving without earning a rate limit.
    """

    checkpoint_every: int = 200
    """Messages between two checkpoints of a **full** walk.

    A crash costs at most this many messages of *listing* work; nothing is
    re-archived, because the relational read model remembers what landed.

    It has no meaning for an incremental run, which checkpoints once at the
    end or not at all: its scope may only ever hold a legal starting point,
    and a mid-delta watermark would skip the records after it.
    """

    poll_interval: float = 2.0
    """Seconds the worker waits before looking for a queued job again."""

    lease_seconds: float = 120.0
    """How long a claimed job stays claimed without a heartbeat.

    Generously longer than :attr:`heartbeat_interval`: a worker that is merely
    slow must not have its job stolen while it still holds the graph session.
    """

    heartbeat_interval: float = 10.0
    """Seconds between two lease extensions while a job runs."""

    worker_id: str = Field(default_factory=default_worker_id)
    """Who claims jobs. Distinct per process, so two workers cannot collide."""

    incremental_interval: float = 0.0
    """Seconds between two sweeps for new mail; zero turns the schedule off.

    Off by default because a fresh install must not start talking to somebody's
    mailbox on its own — the first sync is a button a human presses.
    ``app_sync_incremental_interval=900`` turns it on.

    Read by :class:`~mailarc_sync.jobs.scheduler.IntervalScheduler`, which
    ``app/worker.py`` starts beside the poll loop. A sweep only enqueues, so
    this is how often mailboxes are *looked at*, not how long a sync takes.
    """

    supervise_worker: bool = True
    """Whether the web application starts and watches the worker itself.

    True for the desktop app, where there is nobody else to do it. False under
    Docker or systemd, where the worker is its own unit and a second copy
    started by the application would claim the same jobs.
    """
