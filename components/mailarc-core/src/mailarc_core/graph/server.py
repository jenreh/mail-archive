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

from mailarc_core.graph import ownership
from mailarc_core.graph.admin import is_serving
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.runtime import FalkorDBRuntime

logger = logging.getLogger(__name__)

#: How many lines of server output to keep for diagnostics. Module load
#: failures show up here and nowhere else.
_LOG_TAIL_LINES = 50

#: Grace period between SIGTERM and SIGKILL when stopping the process group.
#:
#: Under a second, and that is not a preference: on the desktop this server is
#: stopped from an ASGI lifespan inside a granian worker, and granian sends
#: that worker SIGKILL two seconds after SIGTERM (``workers_kill_timeout=2``,
#: which Reflex hardcodes). The whole shutdown — this server *and* the sync
#: worker — has to fit inside those two seconds or the shutdown is cut off
#: mid-way and whatever had not been reaped yet is orphaned. Measured: at the
#: five seconds this used to be, a reload left a ``redis-server`` behind every
#: time the sync worker was not instantly reapable.
_TERMINATE_GRACE_SECONDS = 0.8

#: How often :meth:`FalkorDBServer._reaped` looks at a server it did not spawn.
_REAP_POLL_INTERVAL_SECONDS = 0.02

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
        self._pid: int | None = None
        self._output_tail: deque[str] = deque(maxlen=_LOG_TAIL_LINES)
        self._owned = False
        self._startup_error: str | None = None
        self._asked_to_stop = False

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

    def request_stop(self) -> None:
        """Ask the server to exit, and return without waiting for it to.

        Split out of :meth:`stop` because the shutdown budget belongs to *both*
        of the application's children together: granian kills the worker that
        runs the lifespan two seconds after asking it to stop, and a shutdown
        that reaps one child before so much as signalling the other spends that
        budget serially. Asked first and reaped afterwards, a server that is
        never reaped has still been told to go.

        Idempotent, like everything else here: a second SIGTERM to a process
        that has already taken the first is not an error, and one to a process
        that has exited is suppressed.
        """
        if self._pid is None or not self._owned or self._asked_to_stop:
            return
        if self._exited():
            return
        self._asked_to_stop = True
        logger.info("Stopping FalkorDB (pid %d)", self._pid)
        self._signal_group(self._pid, signal.SIGTERM)

    def stop(self) -> None:
        """Stop the server if this process owns it, and insist if it will not.

        Owning it covers two cases: one this process spawned, and one a
        previous run left behind that :meth:`_start_local` took over. The
        second has no :class:`~subprocess.Popen` to wait on, which is why the
        reaping below goes through the pid.
        """
        pid = self._pid
        if pid is None or not self._owned:
            self._process = None
            self._pid = None
            return

        self.request_stop()
        if not self._reaped(_TERMINATE_GRACE_SECONDS):
            logger.warning("FalkorDB ignored SIGTERM, sending SIGKILL")
            self._signal_group(pid, signal.SIGKILL)
            self._reaped(_TERMINATE_GRACE_SECONDS)
        ownership.forget(self._data_dir())
        self._owned = False
        self._process = None
        self._pid = None
        self._asked_to_stop = False

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
        if self._owned and self._pid is not None and not self._exited():
            # Ours already — spawned here or taken over from a run that ended.
            # The check covers both, and it has to: a start that fell through
            # to the adoption below would find its own server listening, read
            # its own record, see a supervisor that is alive because it is
            # *this* process, and hand back the ownership it already had.
            logger.debug("FalkorDB already owned by this process")
            return

        if is_serving(self._config.host, self._config.port):
            self._adopt()
            return

        runtime = FalkorDBRuntime.resolve(self._config.runtime_dir)
        data_dir = self._data_dir()
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
        self._pid = self._process.pid
        self._owned = True
        self._asked_to_stop = False
        # Written before the readiness wait rather than after it: a start that
        # times out leaves `stop` to clean up, and a start interrupted between
        # the two would otherwise leave a server no later run could recognise.
        ownership.record(data_dir, self._pid, self._config.port)
        self._drain_output_in_background()
        self._await_ready()

    def _adopt(self) -> None:
        """Take on a server that was already listening when this start ran.

        Two different situations reach this, and telling them apart is the
        whole point of :mod:`mailarc_core.graph.ownership`. A server whose
        supervisor is still alive belongs to that supervisor — a second
        application instance must not stop it, and this is the behaviour that
        was here before. A server whose supervisor is gone is an orphan of a
        run that is over, and adopting *that* one un-owned is what used to make
        a single leaked server permanent: every later start declined to stop
        it, so it held the port until somebody killed it by hand.
        """
        orphan = ownership.orphan_pid(self._data_dir(), self._config.port)
        if orphan is None:
            logger.info("Adopting the FalkorDB already listening on %s", self.endpoint)
            self._owned = False
            self._pid = None
            return

        logger.warning(
            "Taking over the orphaned FalkorDB on %s (pid %d) — its supervisor is gone",
            self.endpoint,
            orphan,
        )
        self._pid = orphan
        self._owned = True
        self._asked_to_stop = False
        ownership.record(self._data_dir(), orphan, self._config.port)

    def _data_dir(self) -> Path:
        """Where this server keeps its dump, and its ownership record with it."""
        return Path(self._config.data_dir).expanduser().resolve()

    def _exited(self) -> bool:
        """Whether the server this handle points at has already gone."""
        if self._process is not None:
            return self._process.poll() is not None
        return self._pid is not None and not ownership.alive(self._pid)

    def _reaped(self, timeout: float) -> bool:
        """Wait up to *timeout* for the server to exit; say whether it did.

        A server this process spawned is waited on, so it leaves no zombie
        behind. One taken over from a previous run is not ours to wait on —
        it was never our child — so its exit is polled for instead.
        """
        if self._process is not None:
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
            return True

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._exited():
                return True
            time.sleep(_REAP_POLL_INTERVAL_SECONDS)
        return self._exited()

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
    def _signal_group(pid: int, sig: int) -> None:
        """Signal the whole session the server was started in.

        By pid rather than by handle, because a server taken over from a
        previous run has no handle here — it is not this process's child.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), sig)
