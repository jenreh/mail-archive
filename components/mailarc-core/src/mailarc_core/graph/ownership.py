"""Who started the local graph server, written where the next start can read it.

A vendored ``redis-server`` outlives the process that spawned it whenever that
process is killed rather than asked to stop, and on the desktop that is the
ordinary case rather than an accident: the application's supervisor is a
granian worker, and granian sends SIGKILL two seconds after SIGTERM. No signal
follows the parent down, because the server is spawned with
``start_new_session=True`` and is therefore in a session of its own.

The record this module keeps is what makes that recoverable. It names the
server *and* the process supervising it, so a later start can tell the two
cases apart:

* the supervisor is still alive — somebody else runs this server, so adopt it
  and leave it alone. That is what a second application instance must do, and
  it is the behaviour that was here before this module;
* the supervisor is gone — the server is an orphan of a run that is over, so
  take it over and stop it like one this process started.

Without the second case every start after a leak adopted the orphan *un-owned*,
and a Ctrl-C then left it holding the port for good. Measured, not theorised:
one truncated shutdown left a ``redis-server`` on the archive's port that
survived every restart of the application after it.
"""

import logging
import os
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

OWNER_FILENAME = "falkordb.owner"
"""Named after the server rather than the process, and kept in its data dir.

The data directory is what a server *is* here — two of them on one machine are
told apart by the archive they hold open, not by the port, which a second
installation may well reuse.
"""

_COMMAND_TIMEOUT_SECONDS = 2.0

_EXPECTED_COMMAND = "redis-server"
"""What the recorded pid must still be running for a take-over to be allowed.

A pid is recycled, and a stale record naming one that now belongs to somebody
else's process would otherwise turn a start into a SIGTERM at a stranger.
"""


class OwnerRecord(BaseModel):
    """The server, the process supervising it, and the port they agreed on."""

    model_config = ConfigDict(frozen=True)

    server_pid: int
    supervisor_pid: int
    port: int

    @property
    def rendered(self) -> str:
        """The record as the one line that is written to disk."""
        return f"{self.server_pid} {self.supervisor_pid} {self.port}\n"

    @classmethod
    def parsed(cls, text: str) -> OwnerRecord | None:
        """Read one back, or ``None`` if the file is not one of ours.

        A half-written or hand-edited file is not an error worth raising: the
        caller's fallback is the behaviour it had before there was a record.
        """
        parts = text.split()
        if len(parts) != 3:
            return None
        try:
            server_pid, supervisor_pid, port = (int(part) for part in parts)
        except ValueError:
            return None
        return cls(server_pid=server_pid, supervisor_pid=supervisor_pid, port=port)


def owner_path(data_dir: Path) -> Path:
    """Where the record for the server holding *data_dir* lives."""
    return Path(data_dir) / OWNER_FILENAME


def record(data_dir: Path, server_pid: int, port: int) -> None:
    """Write down that this process supervises *server_pid*.

    A failure to write is logged and swallowed: the record buys a cleaner
    recovery from a leak, and a start that cannot have it is still a start.
    """
    entry = OwnerRecord(server_pid=server_pid, supervisor_pid=os.getpid(), port=port)
    try:
        owner_path(data_dir).write_text(entry.rendered, encoding="utf-8")
    except OSError:
        logger.warning("Could not record the graph server's owner", exc_info=True)


def forget(data_dir: Path) -> None:
    """Drop the record, because the server it named has been stopped."""
    try:
        owner_path(data_dir).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not clear the graph server's owner", exc_info=True)


def orphan_pid(data_dir: Path, port: int) -> int | None:
    """The pid of a server left behind by a run that is over, if there is one.

    Every one of the four questions below has to answer yes before a take-over
    is allowed, and the last two are what keep this from signalling a process
    that merely inherited a number.
    """
    entry = _read(data_dir)
    if entry is None or entry.port != port:
        return None
    if alive(entry.supervisor_pid):
        logger.debug(
            "The server on %d is supervised by pid %d", port, entry.supervisor_pid
        )
        return None
    if not alive(entry.server_pid):
        return None
    if not _is_a_server(entry.server_pid):
        logger.debug(
            "Pid %d is no longer a server; ignoring the record", entry.server_pid
        )
        return None
    return entry.server_pid


def alive(pid: int) -> bool:
    """Whether *pid* names a process this machine still has."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Somebody else's process, which is answer enough: it exists.
        return True
    return True


def _read(data_dir: Path) -> OwnerRecord | None:
    try:
        text = owner_path(data_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    return OwnerRecord.parsed(text)


def _is_a_server(pid: int) -> bool:
    """Whether *pid* is still running the command the record was written for.

    The first whitespace-separated token and not the whole line, because the
    two platforms answer differently and FalkorDB rewrites its own title.
    Observed on macOS::

        $ ps -o comm= -p 2598
        /…/src-tauri/resources/falkordb/redis-server 127.0.0.1:6499

    — a full path *and* the title the server set for itself, where Linux gives
    the bare ``redis-server``. Comparing the line cost a take-over the first
    time this ran: the orphan was found, recognised as no server, and adopted
    un-owned all over again.
    """
    try:
        # An absolute path and an integer argument; no user input reaches this.
        result = subprocess.run(  # noqa: S603
            ["/bin/ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.SubprocessError:
        return False
    first = result.stdout.split()
    return bool(first) and Path(first[0]).name == _EXPECTED_COMMAND
