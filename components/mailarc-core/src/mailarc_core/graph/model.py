"""What we know about a graph server.

Pure value objects: no I/O. Everything here describes *what* we know about a
FalkorDB server, never *how* we found it out.

All of them are frozen pydantic models, so a snapshot cannot be edited after
the fact and a value that came from outside is validated on the way in.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

# FalkorDB gained vector indexes (and therefore KNN) in the 4.x line.
MIN_VECTOR_KNN_MAJOR = 4


class GraphServerMode(StrEnum):
    """Where the graph server we talk to comes from.

    ``LOCAL`` means this process owns the server lifecycle (desktop app);
    ``REMOTE`` means someone else runs it (cloud deployment).
    """

    LOCAL = "local"
    REMOTE = "remote"


class GraphBackend(StrEnum):
    """Which Cypher database runic drives.

    The names are runic's own — :func:`runic.ogm.create_driver` takes them
    verbatim. Only ``FALKORDB`` can be run locally: the vendored binaries in
    :mod:`~mailarc_core.graph.runtime` are a redis-server, so every other
    backend is something someone else operates.
    """

    FALKORDB = "falkordb"
    NEO4J = "neo4j"
    MEMGRAPH = "memgraph"
    ARCADEDB = "arcadedb"
    AGE = "age"


class GraphInfo(BaseModel):
    """A single named graph living on the server."""

    model_config = ConfigDict(frozen=True)

    name: str
    node_count: int
    edge_count: int


class ServerMetrics(BaseModel):
    """The handful of server counters worth putting in front of a human."""

    model_config = ConfigDict(frozen=True)

    uptime_seconds: int
    used_memory_human: str
    connected_clients: int
    total_commands_processed: int


class GraphServerStatus(BaseModel):
    """A point-in-time snapshot of a graph server.

    An unreachable server is a perfectly valid status, not an error:
    :func:`mailarc_core.graph.status.read_status` returns ``reachable=False`` with
    ``error`` set rather than raising, so a caller can render "server is down"
    like any other state.
    """

    model_config = ConfigDict(frozen=True)

    mode: GraphServerMode
    endpoint: str
    reachable: bool
    checked_at: datetime
    redis_version: str | None = None
    falkordb_version: str | None = None
    latency_ms: float | None = None
    metrics: ServerMetrics | None = None
    graphs: tuple[GraphInfo, ...] = ()
    error: str | None = None

    @property
    def vector_knn_supported(self) -> bool:
        """Whether this server can serve the KNN queries the project needs."""
        major = _major_version(self.falkordb_version)
        return major is not None and major >= MIN_VECTOR_KNN_MAJOR

    @classmethod
    def unreachable(
        cls,
        mode: GraphServerMode,
        endpoint: str,
        checked_at: datetime,
        error: str,
    ) -> GraphServerStatus:
        """Build the status for a server we could not talk to."""
        return cls(
            mode=mode,
            endpoint=endpoint,
            reachable=False,
            checked_at=checked_at,
            error=error,
        )


def _major_version(version: str | None) -> int | None:
    """Extract the leading major number from a dotted version string."""
    if not version:
        return None
    major, _, _ = version.partition(".")
    try:
        return int(major)
    except ValueError:
        return None
