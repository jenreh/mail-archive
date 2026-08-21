"""Mail archive core.

Everything the application does that does not involve a browser, split by
concern: :mod:`mailarc_core.graph` owns the FalkorDB server's lifecycle and
status, :mod:`mailarc_core.archive` owns what gets written into it,
:mod:`mailarc_core.mail` owns the vocabulary both of them speak, and
:mod:`mailarc_core.database` owns the SQLite wiring. Importing this package
must never pull in Reflex.

The names below belong to the graph and archive packages, re-exported so the
application can say ``from mailarc_core import FalkorDBServer``. The other two
are reached as packages — ``from mailarc_core.mail import parse_message``,
``from mailarc_core.database import sqlite`` — because they are a vocabulary
and a piece of infrastructure rather than a handful of values.
"""

from mailarc_core.archive import (
    ArchiveConfig,
    ArchiveReader,
    ArchiveResult,
    ArchiveSource,
    BlobKind,
    BlobStore,
    MessageArchiver,
)
from mailarc_core.graph import (
    FalkorDBRuntime,
    FalkorDBServer,
    GraphBackend,
    GraphConfig,
    GraphInfo,
    GraphRuntimeError,
    GraphServerMode,
    GraphServerStatus,
    ServerMetrics,
    read_status,
    read_status_async,
)

__all__ = [
    "ArchiveConfig",
    "ArchiveReader",
    "ArchiveResult",
    "ArchiveSource",
    "BlobKind",
    "BlobStore",
    "FalkorDBRuntime",
    "FalkorDBServer",
    "GraphBackend",
    "GraphConfig",
    "GraphInfo",
    "GraphRuntimeError",
    "GraphServerMode",
    "GraphServerStatus",
    "MessageArchiver",
    "ServerMetrics",
    "read_status",
    "read_status_async",
]
