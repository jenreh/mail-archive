"""The two child processes this application owns, and what a failure means.

Split out of :mod:`app.composition`, which was over the thousand-line limit
(§1.6), and the seam is the subject rather than the line count. Everything
else in that module answers "build this component from configuration"; nothing
here is a component. These are *processes* — a vendored redis-server and a
second Python interpreter — and what this module holds is their lifetime and
one policy about them, which is the same policy twice:

**A failed start is logged and swallowed, never raised.** The application is
more useful up than down in both cases. Without a graph the page whose whole
job is reporting server state still answers, and it reports the reason; without
a worker every page that shows what the archive holds keeps working and a job
simply waits in the queue until one exists. A lifespan that raised would turn
either into a blank window.

**Stopping is asked of both before it is waited on for either**, and there is
one lifespan rather than two for exactly that. The supervisor of this process
in dev is a granian worker that Reflex starts with ``workers_kill_timeout=2``,
so a shutdown has two seconds before it is killed where it stands. Reaped one
after the other, the first child could spend all of it and the second was
orphaned without ever being signalled — in its own session, with nothing left
that would signal it. Both handles therefore carry a ``request_stop`` beside
``stop``, and the grace periods are set so that both reaps fit inside the two
seconds as well.

Both handles are cached for the same reason: a second one would be a second
child — a leaked ``redis-server`` per request, or a second worker claiming the
same jobs.

The configuration still comes from :mod:`app.composition`, imported at module
scope in one direction only: that module imports *this* one lazily, inside the
functions that re-export these names, so the pair does not close a cycle at
import time.
"""

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from functools import lru_cache

from appkit_commons.registry import service_registry

from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.health import GraphHealth
from mailarc_core.graph.model import GraphServerStatus
from mailarc_core.graph.server import FalkorDBServer
from mailarc_core.graph.status import read_status_async
from mailarc_sync.engine import SyncConfig

logger = logging.getLogger(__name__)


def _graph_config() -> GraphConfig:
    """The graph configuration, fetched late.

    The import is inside the function rather than at module scope because
    :mod:`app.composition` re-exports the names below, and naming it at the top
    would close the import cycle before either module finished loading — the
    same device ``_reindex`` uses there.
    """
    from app.composition import graph_config

    return graph_config()


def _sync_config() -> SyncConfig:
    """The sync configuration, fetched late. See :func:`_graph_config`."""
    from app.composition import sync_config

    return sync_config()


@lru_cache(maxsize=1)
def graph_server() -> FalkorDBServer:
    """The application-wide graph server handle.

    Cached deliberately: a local server is a real child process, and one per
    caller would leak a redis-server for every request.
    """
    return FalkorDBServer(_graph_config())


async def graph_status() -> GraphServerStatus:
    """Read a fresh snapshot of the graph server."""
    return await read_status_async(_graph_config())


def graph_startup_error() -> str | None:
    """Why the graph server failed to start, if it did."""
    return graph_server().startup_error


@lru_cache(maxsize=1)
def graph_health() -> GraphHealth:
    """The two questions the status page asks, behind one object.

    A façade over the pair this module holds — the configuration a
    fresh snapshot is read over, and the handle that knows why a start failed.
    Cached like the other handles: it owns nothing, but it is one decision and
    two objects would be two answers to "which server is being reported on".
    """
    return GraphHealth(_graph_config(), graph_server())


WORKER_COMMAND = (sys.executable, "-m", "app.worker")
"""How the worker is started: a fresh interpreter, not a thread of this one.

An import runs for hours and holds a graph session; sharing an event loop with
the web application would make every page wait on it. ``-m app.worker`` rather
than an import, because that module is the worker's own entry point and its
process has no business holding Reflex.
"""

STARTUP_GRACE_SECONDS = 0.5
"""How long a start waits to see whether the child dies on the spot.

A worker that cannot import its dependencies is gone in milliseconds, and a
start that never looks would leave the application silently jobless.
"""

TERMINATE_GRACE_SECONDS = 1.0
"""Between SIGTERM and SIGKILL. The worker puts its job down on SIGTERM.

One second, and the figure is granian's rather than this application's. In dev
the lifespan below runs inside a granian worker that Reflex starts with
``workers_kill_timeout=2``, so *this* process is killed two seconds after it is
asked to stop — and the budget covers the graph server as well. Anything left
un-reaped when that lands is orphaned, in its own session, with nothing left
that would signal it.

Abandoning the job is safe and is the design: the lease expires,
:meth:`JobQueue.reclaim_expired` puts the job back, and the next start resumes
at its checkpoint. ``mailarc_sync.jobs.worker`` says so itself — "that is the
same path a ``kill -9`` takes, and one path is worth more than two".
"""


class WorkerProcess:
    """The sync worker as a child of the application that started it.

    Same shape as :class:`~mailarc_core.graph.server.FalkorDBServer`, for the
    same reason: idempotent start and stop so a lifespan may fire twice, and a
    startup failure kept as text instead of thrown away, so the caller decides
    what a missing worker means.

    ``command`` is a parameter so a test can drive these mechanics against a
    child it controls; nothing in the application passes it.
    """

    def __init__(self, command: Sequence[str] = WORKER_COMMAND) -> None:
        self._command = tuple(command)
        self._process: subprocess.Popen[bytes] | None = None
        self._startup_error: str | None = None
        self._asked_to_stop = False

    @property
    def running(self) -> bool:
        """Whether the child is alive right now."""
        return self._process is not None and self._process.poll() is None

    @property
    def startup_error(self) -> str | None:
        """Why the last start failed, if it did."""
        return self._startup_error

    def start(self) -> None:
        """Start the worker unless it is already running."""
        if self.running:
            logger.debug("The sync worker is already running")
            return
        self._asked_to_stop = False
        try:
            self._spawn()
        except Exception as exc:
            self._startup_error = str(exc)
            raise
        else:
            self._startup_error = None

    def request_stop(self) -> None:
        """Ask the worker to finish, and return without waiting for it to.

        Split out of :meth:`stop` for the reason
        :meth:`~mailarc_core.graph.server.FalkorDBServer.request_stop` is: the
        two children share one two-second budget, so both are asked before
        either is waited on. A shutdown cut short then leaves children that are
        already on their way out rather than children that never heard.
        """
        process = self._process
        if process is None or process.poll() is not None or self._asked_to_stop:
            return
        self._asked_to_stop = True
        logger.info("Stopping the sync worker (pid %d)", process.pid)
        self._signal_group(process, signal.SIGTERM)

    def stop(self) -> None:
        """Ask the worker to finish, and insist if it will not."""
        self.request_stop()
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return

        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("The sync worker ignored SIGTERM, sending SIGKILL")
            self._signal_group(process, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=TERMINATE_GRACE_SECONDS)

    async def start_async(self) -> None:
        """:meth:`start` off the event loop; starting a process blocks."""
        await asyncio.to_thread(self.start)

    async def stop_async(self) -> None:
        """:meth:`stop` off the event loop; reaping a process blocks."""
        await asyncio.to_thread(self.stop)

    def _spawn(self) -> None:
        logger.info("Starting the sync worker: %s", " ".join(self._command))
        # The command is this interpreter plus a module name from our own
        # source; no user input reaches it. `start_new_session` gives the
        # worker its own process group, so `stop` takes down whatever it
        # started along with it.
        process = subprocess.Popen(self._command, start_new_session=True)  # noqa: S603
        self._process = process
        try:
            process.wait(timeout=STARTUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return
        raise RuntimeError(
            f"the sync worker exited immediately with code {process.returncode}"
        )

    @staticmethod
    def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), sig)


@lru_cache(maxsize=1)
def sync_worker() -> WorkerProcess:
    """The one worker child this application supervises.

    Cached for the reason :func:`graph_server` is: a second handle would be a
    second child claiming the same jobs.
    """
    return WorkerProcess()


@contextlib.asynccontextmanager
async def supervised_children_lifespan() -> AsyncIterator[None]:
    """ASGI lifespan hook: own both children for as long as the app runs.

    One hook and not two, and the reason is arithmetic rather than taste. In
    dev this runs inside a granian worker that Reflex starts with
    ``workers_kill_timeout=2``: two seconds after the shutdown begins, *this*
    process is killed. Two lifespans unwind one after the other, so the second
    child was not signalled until the first had been reaped — and the first
    alone could spend the whole budget. Measured, not theorised: a hot reload
    reached "stopping the sync worker", was killed at two seconds, and left a
    ``redis-server`` holding the archive's port with nothing left in the system
    that would ever stop it.

    So the shutdown here asks **both** children to go before it waits for
    either. Whatever a truncated shutdown leaves behind is then a child that
    has already had its SIGTERM, rather than one that never heard.

    The starts keep the order and the policy each of them had. The graph server
    comes up first because the worker opens a session against it, and a failed
    start on either side is logged and swallowed rather than taking the
    application down: the page whose whole job is reporting server state is
    more useful up than down (it shows :func:`graph_startup_error`), and every
    page that reads the archive keeps working without a worker — a job simply
    waits in the queue until one exists.

    ``sync.supervise_worker`` turns the worker half off where somebody else
    already runs it: under Docker or systemd it is its own unit, and a second
    copy started here would claim the same jobs.
    """
    server = graph_server()
    try:
        await server.start_async()
    except Exception:
        logger.exception("Could not start the graph server")

    worker = sync_worker() if _sync_config().supervise_worker else None
    if worker is None:
        logger.info("Not supervising the sync worker — something else owns it")
    else:
        try:
            await worker.start_async()
        except Exception:
            logger.exception("Could not start the sync worker")

    try:
        yield
    finally:
        # Both requests first — neither blocks — and only then the waiting.
        if worker is not None:
            worker.request_stop()
        server.request_stop()
        if worker is not None:
            await worker.stop_async()
        await server.stop_async()


def publish_graph_health() -> GraphHealth:
    """Leave the façade where the status page can find it.

    Same route and same reason as :func:`publish_archive_reader`, down to the
    second call being a no-op. This is the one that let a Reflex state leave
    ``app/``: ``GraphStatusState`` used to import :func:`graph_status` and
    :func:`graph_startup_error` from this module by name, which is a component
    importing the application.
    """
    services = service_registry()
    health = graph_health()
    if services.has(GraphHealth) and services.get(GraphHealth) is health:
        return health
    services.register_as(GraphHealth, health)
    return health
