"""Composition root for the graph slice.

The only module in the web application that builds the core's collaborators
from configuration. Everything else asks this module and never constructs a
server or a connection itself.
"""

import contextlib
import logging
from collections.abc import AsyncIterator
from functools import lru_cache

from appkit_commons.registry import service_registry

from mailarc_core import (
    FalkorDBServer,
    GraphConfig,
    GraphServerStatus,
    read_status_async,
)

logger = logging.getLogger(__name__)


def graph_config() -> GraphConfig:
    config = service_registry().get(GraphConfig)
    if config is None:
        raise RuntimeError(
            "GraphConfig is not registered — was app.configuration.configure() called?"
        )
    return config


@lru_cache(maxsize=1)
def graph_server() -> FalkorDBServer:
    """The application-wide graph server handle.

    Cached deliberately: a local server is a real child process, and one per
    caller would leak a redis-server for every request.
    """
    return FalkorDBServer(graph_config())


async def graph_status() -> GraphServerStatus:
    """Read a fresh snapshot of the graph server."""
    return await read_status_async(graph_config())


def graph_startup_error() -> str | None:
    """Why the graph server failed to start, if it did."""
    return graph_server().startup_error


@contextlib.asynccontextmanager
async def graph_server_lifespan() -> AsyncIterator[None]:
    """ASGI lifespan hook: own the graph server for as long as the app runs.

    Policy only: the core knows *how* to start and stop without blocking the
    loop, this decides what a failure means. A failed start is logged and
    swallowed rather than killing the app — the page whose whole job is
    reporting server state is more useful up than down, and it shows the
    reason via :func:`graph_startup_error`.
    """
    server = graph_server()
    try:
        await server.start_async()
    except Exception:
        logger.exception("Could not start the graph server")
    try:
        yield
    finally:
        await server.stop_async()
