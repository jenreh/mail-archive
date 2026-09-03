"""Owns — or merely points at — the FalkorDB server the application uses.

One class covers both modes. In ``REMOTE`` mode there is no lifecycle to run,
so ``start`` and ``stop`` are no-ops; in ``LOCAL`` mode this supervises a
server started from the vendored binaries.
"""

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from mailarc_core.graph.admin import is_serving
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.runtime import FalkorDBRuntime

logger = logging.getLogger(__name__)

#: How many lines of server output to keep for diagnostics. Module load
#: failures show up here and nowhere else.
_LOG_TAIL_LINES = 50

#: Grace period between SIGTERM and SIGKILL when stopping the process group.
_TERMINATE_GRACE_SECONDS = 5.0

_READY_POLL_INTERVAL_SECONDS = 0.1


class FalkorDBServer:
    """Starts, adopts, and stops the configured FalkorDB.

    A single instance is held for the lifetime of the application — never one
    per request. ``start`` and ``stop`` are both idempotent so they can be
    driven from ASGI lifespan hooks, which can fire more than once on reload.

    If something is already serving on the configured port, this *adopts* it
    rather than starting a competing server, and ``stop`` then leaves it
    running.
    """

    def __init__(self, config: GraphConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._output_tail: deque[str] = deque(maxlen=_LOG_TAIL_LINES)
        self._owned = False
        self._startup_error: str | None = None

    @property
    def endpoint(self) -> str:
        return self._config.endpoint

    @property
    def owned(self) -> bool:
        """Whether this process started the server it is talking to."""
        return self._owned

    @property
    def startup_error(self) -> str | None:
        """Why the last ``start`` failed, if it did.

        A status panel prefers this over a bare "connection refused": a
        missing vendored runtime is far more actionable than the symptom.
        """
        return self._startup_error

    def start(self) -> None:
        """Ensure the server is running and accepting connections.

        **A start that fails leaves nothing running.** ``_start_local``
        ``Popen``s the server and only then waits for it to answer, so every
        failure after that line — a readiness timeout, a ``KeyboardInterrupt``,
        a test runner's own timeout landing mid-wait — would otherwise leave a
        redis-server holding the port with no handle anywhere that admits to
        owning it. Measured: an exception raised during ``start`` left a
        vendored server alive after the process that spawned it had exited.

        ``BaseException`` rather than ``Exception`` for exactly that reason:
        the two interesting interruptions here are not ``Exception``\\ s. The
        cleanup cannot swallow them, so the raise below is unconditional.

        :meth:`stop` is safe to call in every one of these states — it is a
        no-op when nothing was spawned, when the server was adopted rather than
        started, and when the process has already exited.
        """
        if self._config.mode is not GraphServerMode.LOCAL:
            logger.info("Using the remote FalkorDB at %s", self.endpoint)
            return
        try:
            self._start_local()
        except BaseException as exc:
            self.stop()
            self._startup_error = str(exc)
            raise
        else:
            self._startup_error = None

    def stop(self) -> None:
        """Stop the server if this process started it."""
        process = self._process
        self._process = None
        if process is None or not self._owned:
            return
        if process.poll() is not None:
            return

        logger.info("Stopping FalkorDB (pid %d)", process.pid)
        self._signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("FalkorDB ignored SIGTERM, sending SIGKILL")
            self._signal_group(process, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        self._owned = False

    async def start_async(self) -> None:
        """:meth:`start` off the event loop; starting a process blocks.

        Failures propagate unchanged — whether a graph server that will not
        come up is fatal is the caller's policy, not this class's.
        """
        await asyncio.to_thread(self.start)

    async def stop_async(self) -> None:
        """:meth:`stop` off the event loop; reaping a process blocks."""
        await asyncio.to_thread(self.stop)

    def _start_local(self) -> None:
        if self._process is not None and self._process.poll() is None:
            logger.debug("FalkorDB already started by this process")
            return

        if is_serving(self._config.host, self._config.port):
            logger.info("Adopting the FalkorDB already listening on %s", self.endpoint)
            self._owned = False
            return

        runtime = FalkorDBRuntime.resolve(self._config.runtime_dir)
        data_dir = Path(self._config.data_dir).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        # The graph persists a parsed rendering of private mail. An explicit
        # chmod rather than a mkdir mode, so a permissive umask cannot leave
        # the dump readable to other users.
        data_dir.chmod(0o700)

        command = [
            str(runtime.redis_server),
            "--port",
            str(self._config.port),
            "--bind",
            self._config.host,
            "--protected-mode",
            "yes",
            "--daemonize",
            "no",
            "--dir",
            str(data_dir),
            "--dbfilename",
            "falkordb.rdb",
            "--loadmodule",
            str(runtime.module),
        ]
        logger.info("Starting FalkorDB: %s", " ".join(command))

        # The command is built entirely from vendored, validated paths and
        # configuration values; no user input reaches it. `start_new_session`
        # puts the server in its own process group so `stop` can take down any
        # children it spawns along with it.
        self._process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._owned = True
        self._drain_output_in_background()
        self._await_ready()

    def _await_ready(self) -> None:
        deadline = time.monotonic() + self._config.startup_timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise self._startup_failure("exited during startup")
            if is_serving(self._config.host, self._config.port):
                logger.info("FalkorDB ready on %s", self.endpoint)
                return
            time.sleep(_READY_POLL_INTERVAL_SECONDS)
        self.stop()
        raise self._startup_failure(
            f"not ready within {self._config.startup_timeout:g}s"
        )

    def _startup_failure(self, reason: str) -> RuntimeError:
        tail = "\n".join(self._output_tail) or "<no output captured>"
        return RuntimeError(f"FalkorDB {reason}. Server output:\n{tail}")

    def _drain_output_in_background(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        def drain() -> None:
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                self._output_tail.append(line)
                logger.debug("falkordb: %s", line)

        threading.Thread(target=drain, daemon=True, name="falkordb-log").start()

    @staticmethod
    def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), sig)
