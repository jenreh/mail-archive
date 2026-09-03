"""The FalkorDB-shaped corner runic's protocols do not reach.

runic speaks Cypher. Everything here is something else — a Redis ``PING``,
``INFO``, ``MODULE LIST``, ``GRAPH.LIST``, and the one connection quirk
:class:`~runic.ogm.FalkorDBDriver` leaves to its caller — so none of it works
against Neo4j, Memgraph, ArcadeDB or AGE.

Keeping it in one named module is the point: :mod:`~mailarc_core.graph.client`
is then free of FalkorDB entirely, and a caller that reaches in here can see it
is leaving the portable path. Check ``config.backend`` before you do.
"""

import contextlib
import logging
from typing import Any, cast

from falkordb import FalkorDB
from runic.ogm import FalkorDBDriver, GraphDriver

from mailarc_core.graph.model import ServerMetrics

logger = logging.getLogger(__name__)

_PING_TIMEOUT_SECONDS = 1
"""Bounds both halves of the probe below — reaching the host, and hearing back.

Both, because they fail differently and only one of them is the obvious one. A
connect timeout covers an address nothing answers at; a **read** timeout covers
an address something answers at and then says nothing, which is what a VPN, a
corporate firewall, a draining load balancer and a wedged server all look like
from here. Without the second, :func:`is_serving` blocks forever on exactly
those, and it is called from
:meth:`~mailarc_core.graph.server.FalkorDBServer._await_ready`, whose
``startup_timeout`` is checked *between* probes — so one probe that never
returns is a desktop application that never finishes starting and never says
why. Measured on a network that accepts connections to unrouted addresses:
without a read timeout the PING below hung indefinitely; with one it fails in
1.01s.
"""

#: FalkorDB reports its module version as major*10000 + minor*100 + patch.
_VERSION_MAJOR_SCALE = 10_000
_VERSION_MINOR_SCALE = 100


def is_serving(host: str, port: int) -> bool:
    """Whether a FalkorDB answers PING at this address.

    A liveness probe for :mod:`~mailarc_core.graph.server`, not a query: it
    has to answer before there is a graph worth opening a driver on, so it
    never goes through runic.

    **Always returns**, which is the whole contract and the reason both
    timeouts are set — see :data:`_PING_TIMEOUT_SECONDS`. A probe that can
    block has no answer for its caller's deadline.
    """
    try:
        db = FalkorDB(
            host=host,
            port=port,
            socket_connect_timeout=_PING_TIMEOUT_SECONDS,
            socket_timeout=_PING_TIMEOUT_SECONDS,
        )
        try:
            return bool(db.connection.ping())
        finally:
            with contextlib.suppress(Exception):
                db.close()
    except Exception:
        return False


def release(driver: GraphDriver) -> None:
    """Close the redis handle runic's FalkorDB driver leaves open.

    Every other runic driver really closes its connection in ``close()``;
    ``FalkorDBDriver.close`` is a no-op, so without this a status poll leaks a
    socket per refresh. A no-op for every other backend, which is why
    :func:`~mailarc_core.graph.client.close` can call it unconditionally.
    """
    if not isinstance(driver, FalkorDBDriver):
        return
    with contextlib.suppress(Exception):
        connection(driver).close()


def connection(driver: GraphDriver) -> FalkorDB:
    """The raw FalkorDB handle underneath a driver.

    ``INFO``, ``MODULE LIST`` and ``GRAPH.LIST`` are Redis administration
    rather than Cypher, so runic has nothing to say about them. This is the
    one sanctioned way past the OGM.
    """
    if not isinstance(driver, FalkorDBDriver):
        raise TypeError(
            f"{type(driver).__name__} is not a FalkorDB driver — "
            "check config.backend before reaching for the redis handle"
        )
    db, _ = driver.falkordb_connection()
    return db


def graph_names(driver: GraphDriver) -> tuple[str, ...]:
    """Every graph the server holds.

    A FalkorDB keeps many graphs behind one endpoint; the bolt-speaking
    backends give a driver exactly one, and have no equivalent listing.
    """
    return tuple(str(name) for name in connection(driver).list_graphs() or [])


def driver_for(driver: GraphDriver, graph_name: str) -> GraphDriver:
    """A second driver bound to another graph on the same connection.

    A runic driver addresses exactly one graph, and the status inventory has
    to count every graph on the server. Re-using the open connection beats
    dialling once per graph.
    """
    db = connection(driver)
    return FalkorDBDriver(db.select_graph(graph_name), db)


def server_facts(driver: GraphDriver) -> dict[str, Any]:
    """The versions and metrics a FalkorDB reports about itself.

    Shaped as the keyword arguments
    :class:`~mailarc_core.graph.model.GraphServerStatus` wants, because every
    one of them is a field only this backend can fill. Raising means the
    server could not be reached — which is how ``read_status`` finds out.
    """
    db = connection(driver)
    info = _info(db)
    return {
        "redis_version": info.get("redis_version"),
        "falkordb_version": _module_version(db),
        "metrics": _metrics(info),
    }


def _info(db: FalkorDB) -> dict[str, Any]:
    """Read ``INFO``.

    redis-py declares ``info()`` as possibly awaitable because one signature
    covers its sync and async clients; the sync client's return never is.
    """
    return cast("dict[str, Any]", db.connection.info())


def _metrics(info: dict[str, Any]) -> ServerMetrics:
    return ServerMetrics(
        uptime_seconds=int(info.get("uptime_in_seconds", 0)),
        used_memory_human=str(info.get("used_memory_human", "unknown")),
        connected_clients=int(info.get("connected_clients", 0)),
        total_commands_processed=int(info.get("total_commands_processed", 0)),
    )


def _module_version(db: Any) -> str | None:
    """Read the FalkorDB module version and render it as a dotted string."""
    try:
        modules = db.connection.module_list()
    except Exception:
        logger.debug("MODULE LIST unavailable", exc_info=True)
        return None

    for module in modules or []:
        name, version = _module_entry(module)
        if name in ("graph", "falkordb") and version is not None:
            return _format_version(version)
    return None


def _module_entry(module: Any) -> tuple[str | None, int | None]:
    """Normalise one MODULE LIST entry to ``(name, version)``.

    redis-py hands back dicts, but a flat ``[key, value, ...]`` list still
    turns up depending on the server and protocol in use.
    """
    if isinstance(module, dict):
        raw = module
    elif isinstance(module, (list, tuple)):
        raw = dict(zip(module[::2], module[1::2], strict=False))
    else:
        return None, None

    raw_name = raw.get("name") or raw.get(b"name")
    raw_version = raw.get("ver") or raw.get(b"ver")

    name = str(raw_name) if raw_name is not None else None
    try:
        version = int(raw_version) if raw_version is not None else None
    except TypeError, ValueError:
        version = None
    return name, version


def _format_version(version: int) -> str:
    major, remainder = divmod(version, _VERSION_MAJOR_SCALE)
    minor, patch = divmod(remainder, _VERSION_MINOR_SCALE)
    return f"{major}.{minor}.{patch}"
