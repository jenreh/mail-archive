"""What an archive occupies on disk.

Pure value objects: no I/O, and nothing here knows how the numbers were
arrived at — :mod:`mailarc_core.storage.usage` does the walking. Frozen
pydantic models, so a measurement cannot be edited after the fact.

The derived properties exist because the alternative is arithmetic in a
template. A storage panel wants a ratio to bind a bar to, and the ratio's
denominator is regularly nought: a mailstore that has not been written yet has
no filesystem to report, and every path a reader could not measure comes back
as zeros by design. Dividing there is the one way this module can crash, so it
is the one thing it refuses to do.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

_PERCENT = 100.0


class PathUsage(BaseModel):
    """One measured path, and the filesystem it sits on.

    ``used_bytes`` and ``file_count`` are the path's own; ``total_bytes`` and
    ``free_bytes`` belong to the whole volume, straight from
    :func:`shutil.disk_usage`. Keeping both means a panel can say "the
    mailstore holds 4.2 GB" *and* "the disk it is on is 61 % full" without a
    second read.

    All four default to nought, because that is what an unreadable or absent
    path honestly measures.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    path: Path
    used_bytes: int = 0
    file_count: int = 0
    total_bytes: int = 0
    free_bytes: int = 0

    @property
    def used_ratio(self) -> float:
        """What this path takes of its filesystem, between 0.0 and 1.0.

        ``0.0`` when the total is unknown rather than a ``ZeroDivisionError``:
        "we could not measure it" and "it is empty" render the same, and
        neither is worth a broken page.
        """
        if self.total_bytes <= 0:
            return 0.0
        return self.used_bytes / self.total_bytes

    @property
    def used_percent(self) -> float:
        """:attr:`used_ratio` as the number a progress component wants."""
        return self.used_ratio * _PERCENT


class StorageUsage(BaseModel):
    """Every path one reader was asked to measure, in the order it was asked.

    Order is the panel's row order, which is why this is a tuple and not a
    mapping: the composition root decides that mailstore comes before graph
    comes before database, and nothing downstream should have to re-decide it.
    """

    model_config = ConfigDict(frozen=True)

    paths: tuple[PathUsage, ...] = ()

    @property
    def used_bytes(self) -> int:
        """What the archive occupies in total, across every measured path."""
        return sum(path.used_bytes for path in self.paths)

    @property
    def file_count(self) -> int:
        """How many files the archive occupies in total."""
        return sum(path.file_count for path in self.paths)
