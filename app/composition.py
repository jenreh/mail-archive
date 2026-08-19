"""Composition root: what the application builds, and who owns it afterwards.

The only module in the web application that builds a component from
configuration. Everything else asks this module and never constructs a server,
a connection or a provider itself — which is also why this is the one file that
is allowed to name an implementation (§4.1).
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

from mailarc_core import (
    ArchiveConfig,
    FalkorDBServer,
    GraphConfig,
    GraphServerStatus,
    read_status_async,
)
from mailarc_core.mail.config import MailConfig
from mailarc_sync.engine import (
    FAKE_DESCRIPTOR,
    FakeMailSource,
    ProviderRegistry,
    SyncConfig,
)

logger = logging.getLogger(__name__)


def _registered[T](config: type[T]) -> T:
    """One configuration object, or the sentence that says what is missing."""
    instance = service_registry().get(config)
    if instance is None:
        raise RuntimeError(
            f"{config.__name__} is not registered — "
            "was app.configuration.configure() called?"
        )
    return instance


def graph_config() -> GraphConfig:
    return _registered(GraphConfig)


def sync_config() -> SyncConfig:
    return _registered(SyncConfig)


def archive_config() -> ArchiveConfig:
    return _registered(ArchiveConfig)


def mail_config() -> MailConfig:
    return _registered(MailConfig)


@lru_cache(maxsize=1)
def graph_server() -> FalkorDBServer:
    """The application-wide graph server handle.

    Cached deliberately: a local server is a real child process, and one per
    caller would leak a redis-server for every request.
    """
    return FalkorDBServer(graph_config())


async def graph_status() -> GraphServerStatus:
    """Read a fresh snapshot of the graph server."""
    return await read_status_async(graph_config())


def graph_startup_error() -> str | None:
    """Why the graph server failed to start, if it did."""
    return graph_server().startup_error


@contextlib.asynccontextmanager
async def graph_server_lifespan() -> AsyncIterator[None]:
    """ASGI lifespan hook: own the graph server for as long as the app runs.

    Policy only: the core knows *how* to start and stop without blocking the
    loop, this decides what a failure means. A failed start is logged and
    swallowed rather than killing the app — the page whose whole job is
    reporting server state is more useful up than down, and it shows the
    reason via :func:`graph_startup_error`.
    """
    server = graph_server()
    try:
        await server.start_async()
    except Exception:
        logger.exception("Could not start the graph server")
    try:
        yield
    finally:
        await server.stop_async()


@lru_cache(maxsize=1)
def provider_registry() -> ProviderRegistry:
    """Every mailbox kind this installation can actually open.

    The registry is a singleton because it is a list of decisions, not state:
    building a second one would give two answers to "which providers exist".
    """
    registry = ProviderRegistry()
    registry.register(FAKE_DESCRIPTOR, FakeMailSource.create)
    # GmailSource is registered here in phase 3 — one line, and nothing below
    # this module learns a provider's name.
    return registry


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

TERMINATE_GRACE_SECONDS = 10.0
"""Between SIGTERM and SIGKILL. The worker puts its job down on SIGTERM."""


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
        try:
            self._spawn()
        except Exception as exc:
            self._startup_error = str(exc)
            raise
        else:
            self._startup_error = None

    def stop(self) -> None:
        """Ask the worker to finish, and insist if it will not."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return

        logger.info("Stopping the sync worker (pid %d)", process.pid)
        self._signal_group(process, signal.SIGTERM)
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
async def sync_worker_lifespan() -> AsyncIterator[None]:
    """ASGI lifespan hook: own the sync worker for as long as the app runs.

    Policy only, and the same policy the graph server gets: a failed start is
    logged and swallowed rather than killing the application — the pages that
    show what the archive holds keep working without a worker, and a job simply
    waits in the queue until one exists.

    ``sync.supervise_worker`` turns this off where somebody else already runs
    the worker: under Docker or systemd it is its own unit, and a second copy
    started here would claim the same jobs.
    """
    if not sync_config().supervise_worker:
        logger.info("Not supervising the sync worker — something else owns it")
        yield
        return

    worker = sync_worker()
    try:
        await worker.start_async()
    except Exception:
        logger.exception("Could not start the sync worker")
    try:
        yield
    finally:
        await worker.stop_async()
