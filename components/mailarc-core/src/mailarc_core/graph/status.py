"""Turns a live graph server into a status snapshot.

Deliberately mode-agnostic: the same code serves the desktop app's own server
and a managed cloud one, so nothing downstream branches on where the server
came from. Drivers come from :mod:`~mailarc_core.graph.client` and are held as
:class:`runic.ogm.GraphDriver`; the value objects handed back come from
:mod:`~mailarc_core.graph.model`.

Reachability and the node/edge counts are plain Cypher and work on any backend
runic drives. The rest of the snapshot — the redis and module versions, the
server metrics, the list of graphs behind one endpoint — is Redis
administration that only FalkorDB answers, so it is read through
:mod:`~mailarc_core.graph.admin` and simply left empty for everyone else.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from runic.ogm import GraphDriver

from mailarc_core.graph import admin
from mailarc_core.graph.client import close, connect
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphBackend, GraphInfo, GraphServerStatus

logger = logging.getLogger(__name__)

_NODE_COUNT_QUERY = "MATCH (n) RETURN count(n)"
_EDGE_COUNT_QUERY = "MATCH ()-[r]->() RETURN count(r)"

#: Cheapest statement every Cypher backend accepts. Stands in for ``INFO`` as
#: the reachability probe where there is no Redis to ask.
_REACHABILITY_QUERY = "RETURN 1"


def read_status(config: GraphConfig) -> GraphServerStatus:
    """Collect a status snapshot over a single runic driver.

    Never raises for an unreachable server: an outage is a status, not an
    error, and a status panel has to be able to render it.
    """
    checked_at = datetime.now(UTC)
    started = time.perf_counter()
    driver: GraphDriver | None = None
    try:
        driver = connect(config)
        facts = _server_facts(config, driver)
        latency_ms = (time.perf_counter() - started) * 1000
        return GraphServerStatus(
            mode=config.mode,
            endpoint=config.endpoint,
            reachable=True,
            checked_at=checked_at,
            latency_ms=round(latency_ms, 2),
            graphs=_graphs(config, driver),
            **facts,
        )
    except Exception as exc:  # noqa: BLE001 - any failure is "unreachable"
        logger.debug("Graph server unreachable at %s: %s", config.endpoint, exc)
        return GraphServerStatus.unreachable(
            mode=config.mode,
            endpoint=config.endpoint,
            checked_at=checked_at,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if driver is not None:
            close(driver)


async def read_status_async(config: GraphConfig) -> GraphServerStatus:
    """:func:`read_status` off the event loop; every runic driver blocks."""
    status = await asyncio.to_thread(read_status, config)
    logger.debug(
        "Graph status: reachable=%s endpoint=%s graphs=%d",
        status.reachable,
        status.endpoint,
        len(status.graphs),
    )
    return status


def _server_facts(config: GraphConfig, driver: GraphDriver) -> dict[str, Any]:
    """The versions and metrics only a FalkorDB can supply.

    Doubles as the reachability probe, because reaching the server is exactly
    what it proves: ``INFO`` where there is a Redis to ask, and a trivial
    Cypher statement where there is not. Raising here is how
    :func:`read_status` learns the server is down.
    """
    if config.backend is not GraphBackend.FALKORDB:
        driver.execute(_REACHABILITY_QUERY, {})
        return {}
    return admin.server_facts(driver)


def _graphs(config: GraphConfig, driver: GraphDriver) -> tuple[GraphInfo, ...]:
    """Count every graph the server holds.

    FalkorDB keeps many behind one endpoint and can list them; every other
    backend hands a driver exactly one, so the configured name is the whole
    inventory.
    """
    if config.backend is not GraphBackend.FALKORDB:
        return (_count(driver, config.graph_name),)

    try:
        names = admin.graph_names(driver)
    except Exception:
        logger.debug("GRAPH.LIST unavailable", exc_info=True)
        return ()
    return tuple(_count(admin.driver_for(driver, name), name) for name in names)


def _count(driver: GraphDriver, name: str) -> GraphInfo:
    """Node and edge counts for one graph, or ``-1`` if it cannot be read."""
    try:
        return GraphInfo(
            name=name,
            node_count=_scalar(driver, _NODE_COUNT_QUERY),
            edge_count=_scalar(driver, _EDGE_COUNT_QUERY),
        )
    except Exception:
        # One unreadable graph must not blank out the whole panel.
        logger.debug("Could not inspect graph %s", name, exc_info=True)
        return GraphInfo(name=name, node_count=-1, edge_count=-1)


def _scalar(driver: GraphDriver, query: str) -> int:
    """Count through runic. Writes to a counted graph are not a concern here.

    ``GraphDriver.execute`` issues ``GRAPH.QUERY`` rather than the read-only
    ``GRAPH.RO_QUERY`` the raw client offered, which only matters against a
    replica — and the app talks to a primary.
    """
    rows = driver.execute(query, {}).rows or []
    if not rows or not rows[0]:
        return 0
    return int(rows[0][0])
