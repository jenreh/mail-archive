"""Measuring what the archive occupies, one directory tree at a time.

**Everything here blocks.** Walking a mailstore is thousands of ``stat`` calls
against a real disk, and :func:`shutil.disk_usage` is a syscall of its own, so
an event loop must never call :meth:`StorageReader.usage` directly: the
caller's contract is ``await asyncio.to_thread(reader.usage)``, exactly as
``mailarc_ui``'s review state already does around the blocking archive reads.

Nothing here raises. A storage panel is asked to render on installations where
the mailstore has not been created yet, where the graph directory belongs to
another user, and where a disk went away between two reads — and on all three
the honest answer is nought, reported beside the paths that *could* be
measured. A reader that raised would take the whole panel down to say one
number was unavailable.

Symlinks are never followed out of the tree, and directory symlinks are not
followed at all. A loop back onto an ancestor would otherwise hang the walk,
and a link into somebody's home directory would have the archive report a size
it does not occupy. The root path itself is the exception: if the caller hands
in a symlinked ``store_dir``, that link *is* the tree it asked about.
"""

import logging
import os
import shutil
import stat
import threading
from collections.abc import Mapping
from pathlib import Path
from time import monotonic

from mailarc_core.storage.model import PathUsage, StorageUsage

logger = logging.getLogger(__name__)


def directory_bytes(path: Path) -> tuple[int, int]:
    """Count the files under ``path`` and add up their sizes.

    Returns ``(file_count, total_bytes)``, and ``(0, 0)`` for a path that does
    not exist, is not readable, or fails mid-walk. A plain file counts as one
    file of its own size — the SQLite database is handed to a reader like any
    other path, and making the caller branch on what kind of thing it holds
    would move this decision into three places instead of one.

    Blocks. See the module docstring.
    """
    try:
        root = path.stat()
    except OSError as error:
        logger.debug("Cannot measure %s: %s", path, error)
        return (0, 0)

    if not stat.S_ISDIR(root.st_mode):
        return (1, root.st_size)

    files = 0
    total = 0
    pending: list[Path] = [path]
    while pending:
        found, measured, deeper = _scan(pending.pop())
        files += found
        total += measured
        pending.extend(deeper)
    return (files, total)


def _scan(directory: Path) -> tuple[int, int, list[Path]]:
    """One directory: its files, their bytes, and the subdirectories below it.

    Iterative rather than recursive, so a deep mailstore cannot exhaust the
    stack, and so the symlink rule is stated once: a directory symlink is never
    added to the pending list, and a file symlink is skipped rather than
    ``stat``-ed through to whatever it points at.
    """
    files = 0
    total = 0
    deeper: list[Path] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        deeper.append(Path(entry.path))
                    elif not entry.is_symlink():
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError as error:
                    logger.debug("Cannot measure %s: %s", entry.path, error)
    except OSError as error:
        # One closed subdirectory must not blank out the rest of the tree.
        logger.debug("Cannot list %s: %s", directory, error)
    return (files, total, deeper)


DEFAULT_TTL_SECONDS = 60.0
"""How long one measurement stands before the tree is walked again.

Short enough that the figure on a panel is still a measurement and not a claim
about last week, long enough that a page nobody has to sign in for cannot be
turned into a disk-walking machine by reloading it. A minute is also roughly
the interval at which an import changes the answer by an amount a bar can show.
"""


class StorageReader:
    """Measures the handful of paths one archive lives in.

    Constructed with them, because only the composition root knows all three:
    the mailstore, the graph's data directory and the SQLite file each come
    from a different component's configuration. The mapping's keys are the
    labels a panel prints and its order is the order they are printed in.

    **It remembers its last answer for :data:`DEFAULT_TTL_SECONDS`**, and that
    is a correctness property of the caller rather than an optimisation:
    :meth:`usage` is on the ``on_load`` of the public dashboard, and one call
    walks the whole mailstore. Without the window, every anonymous request
    would cost thousands of ``stat`` calls against a real disk — a denial of
    service anybody can spell. A caller who has just changed what is on disk
    asks with ``fresh=True``.

    The lock is held **across** the walk on purpose: ten visitors arriving at
    once should cost one walk that nine of them wait for, not ten that run side
    by side.
    """

    def __init__(
        self, paths: Mapping[str, Path], *, ttl_seconds: float = DEFAULT_TTL_SECONDS
    ) -> None:
        self._paths = dict(paths)
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._last: StorageUsage | None = None
        self._last_at = 0.0

    def usage(self, *, fresh: bool = False) -> StorageUsage:
        """What every configured path occupies, measured or remembered.

        Blocks — thousands of ``stat`` calls per mailstore on the walk this
        does take. Callers on an event loop use
        ``await asyncio.to_thread(reader.usage)``.
        """
        with self._lock:
            remembered = self._remembered()
            if not fresh and remembered is not None:
                return remembered
            return self._measure()

    def _remembered(self) -> StorageUsage | None:
        """The last answer while it is still inside the window.

        Named apart from the module-level :func:`_measured`, which measures one
        path: an attribute and a function sharing a name is the kind of thing
        that reads fine and edits badly.
        """
        if self._last is None:
            return None
        if monotonic() - self._last_at >= self.ttl_seconds:
            return None
        return self._last

    def _measure(self) -> StorageUsage:
        """Walk every path and keep what came back. Called under the lock."""
        usage = StorageUsage(
            paths=tuple(_measured(label, path) for label, path in self._paths.items())
        )
        self._last = usage
        self._last_at = monotonic()
        logger.debug(
            "Measured %d paths: %d files, %d bytes",
            len(usage.paths),
            usage.file_count,
            usage.used_bytes,
        )
        return usage


def _measured(label: str, path: Path) -> PathUsage:
    """One path's own size and the size of the volume it sits on."""
    file_count, used_bytes = directory_bytes(path)
    total_bytes, free_bytes = _disk_bytes(path)
    return PathUsage(
        label=label,
        path=path,
        used_bytes=used_bytes,
        file_count=file_count,
        total_bytes=total_bytes,
        free_bytes=free_bytes,
    )


def _disk_bytes(path: Path) -> tuple[int, int]:
    """``(total, free)`` of the filesystem ``path`` is on, or ``(0, 0)``.

    :func:`shutil.disk_usage` raises for a path that does not exist, and a
    mailstore that has not been created yet is the ordinary case on a fresh
    installation — so it is answered with the same zeros the walk gives,
    rather than by climbing to an ancestor and reporting a volume the caller
    never asked about.
    """
    try:
        total, _, free = shutil.disk_usage(path)
    except OSError as error:
        logger.debug("Cannot read the filesystem behind %s: %s", path, error)
        return (0, 0)
    return (total, free)
