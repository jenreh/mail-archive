"""The FalkorDB graph store: its settings, its lifecycle, and its state.

Graph data is reached through `runic <https://pypi.org/project/runic-py/>`_'s
OGM — :func:`~mailarc_core.graph.client.session` is the way in — so queries
are written against mapped models rather than hand-rolled Cypher strings, and
against runic's ``GraphDriver`` protocol rather than one vendor's client.
``GraphConfig.backend`` picks which database answers.

The server's *lifecycle* is not runic's business and stays here: the vendored
binaries are a redis-server, so local mode is FalkorDB and only FalkorDB.

One module per concern, layered so nothing points back up:

``model``
    Value objects. No I/O, no imports from anything below.
``config``
    ``GraphConfig`` — where the server is and how it is run.
``runtime``
    Finds and validates the vendored ``redis-server`` and ``falkordb.so``.
``admin``
    The FalkorDB-only operations runic does not cover: PING, INFO, the
    module and graph listings.
``client``
    Builds runic drivers and sessions. Backend-independent.
``status``
    Reads a snapshot; never raises for an unreachable server.
``server``
    ``FalkorDBServer`` — starts, adopts and stops the process.
"""

from mailarc_core.graph.admin import is_serving
from mailarc_core.graph.client import close, connect, session
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import (
    GraphBackend,
    GraphInfo,
    GraphServerMode,
    GraphServerStatus,
    ServerMetrics,
)
from mailarc_core.graph.runtime import FalkorDBRuntime, GraphRuntimeError
from mailarc_core.graph.server import FalkorDBServer
from mailarc_core.graph.status import read_status, read_status_async

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
    "close",
    "connect",
    "is_serving",
    "read_status",
    "read_status_async",
    "session",
]
