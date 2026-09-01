"""How much disk the archive occupies, and how much disk is left.

One capability, the fixed file roles the rest of this component uses:
``model`` holds the value objects and knows no I/O, ``usage`` does the walking
and the ``statvfs``. Nothing here knows *which* paths matter — the mailstore
belongs to :mod:`mailarc_core.archive`, the graph's data directory to
:mod:`mailarc_core.graph` and the database file to appkit, so only the
composition root can name all three, and it hands them to
:class:`StorageReader` when it builds one.

Everything a reader does blocks; callers on an event loop wrap it in
``asyncio.to_thread``.
"""

from mailarc_core.storage.model import PathUsage, StorageUsage
from mailarc_core.storage.usage import StorageReader, directory_bytes

__all__ = [
    "PathUsage",
    "StorageReader",
    "StorageUsage",
    "directory_bytes",
]
