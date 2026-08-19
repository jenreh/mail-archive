"""Mail archive core.

Everything the application does that does not involve a browser, split by
concern: :mod:`mailarc_core.graph` owns the FalkorDB server's lifecycle and
status, :mod:`mailarc_core.database` owns the SQLite wiring. Importing this
package must never pull in Reflex.

The names below are the graph package's, re-exported so the application can
say ``from mailarc_core import FalkorDBServer``. The SQLite side is reached as
``from mailarc_core.database import sqlite`` — it wires infrastructure rather
than being used as a value.
"""

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
    "FalkorDBRuntime",
    "FalkorDBServer",
    "GraphBackend",
    "GraphConfig",
    "GraphInfo",
    "GraphRuntimeError",
    "GraphServerMode",
    "GraphServerStatus",
    "ServerMetrics",
    "read_status",
    "read_status_async",
]
