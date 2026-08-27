"""The two questions a status panel asks about the graph server.

Both answers already existed — :func:`~mailarc_core.graph.status.read_status_async`
gives the snapshot and :attr:`FalkorDBServer.startup_error` gives the reason a
start failed — but they lived in two places, and the state class that needed
them both reached into ``app.composition`` to get them. That import is what
kept a Reflex state stranded in ``app/``: a component may not import the
application.

So this is a façade and nothing more. It holds no state of its own, caches
nothing, and deliberately does not wrap the server's lifecycle: starting and
stopping stays the composition root's policy, and a page that could stop the
database would be a surprise nobody asked for.

Why both a config *and* a server handle: they answer different questions. The
snapshot is read over a fresh driver built from the config, and works just as
well against a server this process never started — a remote FalkorDB in a
cloud deployment. ``startup_error`` is only knowable to the handle that tried,
and is what turns "connection refused" into "the vendored ``falkordb.so`` is
missing", which is the difference between a symptom and something a human can
act on.
"""

import logging

from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerStatus
from mailarc_core.graph.server import FalkorDBServer
from mailarc_core.graph.status import read_status_async

logger = logging.getLogger(__name__)


class GraphHealth:
    """Reads how the graph server is doing, without owning it."""

    def __init__(self, config: GraphConfig, server: FalkorDBServer) -> None:
        self._config = config
        self._server = server

    async def status(self) -> GraphServerStatus:
        """A fresh snapshot of the server.

        Never raises: an unreachable server comes back as ``reachable=False``
        with the reason in ``error``, because an outage is a status a panel has
        to be able to render like any other.
        """
        return await read_status_async(self._config)

    def startup_error(self) -> str | None:
        """Why the last start failed, if it did — read off the handle.

        Read through rather than copied at construction: the handle learns of
        a failure whenever the lifespan hook tries to start it, which is after
        this object exists.
        """
        return self._server.startup_error
