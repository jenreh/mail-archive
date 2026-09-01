"""Settings the core needs, readable without the web application."""

from pathlib import Path
from typing import Any

from appkit_commons.configuration.base import BaseConfig
from pydantic import SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from mailarc_core.graph.model import GraphBackend, GraphServerMode


class GraphConfig(BaseConfig):
    """Connection and runtime settings for the graph store.

    ``LOCAL`` mode means this process starts and supervises the server from the
    vendored binaries; ``REMOTE`` mode means it only connects to one someone
    else runs. ``data_dir``, ``runtime_dir`` and ``startup_timeout`` are
    ignored in remote mode.

    ``backend`` picks the driver runic builds. FalkorDB is the one this project
    ships and the only one that can be supervised locally; the rest are reached
    over the network and need ``database``, ``username`` and ``password``.
    """

    model_config = SettingsConfigDict(
        env_prefix="app_graph_",
        env_file=".env",
        populate_by_name=True,
    )

    mode: GraphServerMode = GraphServerMode.LOCAL
    backend: GraphBackend = GraphBackend.FALKORDB
    host: str = "127.0.0.1"
    port: int = 6379
    graph_name: str = "mail-archive"
    database: str | None = None
    username: str | None = None
    password: SecretStr | None = None
    startup_timeout: float = 15.0
    data_dir: Path = Path(".state/falkordb")
    runtime_dir: Path | None = None

    @property
    def endpoint(self) -> str:
        """Human-readable ``host:port`` of the server."""
        return f"{self.host}:{self.port}"

    def driver_options(self) -> dict[str, Any]:
        """The keyword arguments :func:`runic.ogm.create_driver` wants.

        Each backend names its target differently: FalkorDB and AGE address a
        *graph* on a server, the bolt-speaking ones address a *database* and
        authenticate. ``database`` falls back to ``graph_name`` so a single
        setting still names the thing being queried.
        """
        options: dict[str, Any] = {"host": self.host, "port": self.port}
        if self.backend in (GraphBackend.FALKORDB, GraphBackend.AGE):
            options["graph"] = self.graph_name
        if self.backend is not GraphBackend.FALKORDB:
            options["database"] = self.database or self.graph_name
            options["username"] = self.username or ""
            options["password"] = (
                self.password.get_secret_value() if self.password else ""
            )
        return options

    @model_validator(mode="after")
    def _only_falkordb_runs_locally(self) -> GraphConfig:
        """Local mode supervises a redis-server; nothing else fits behind it."""
        if (
            self.mode is GraphServerMode.LOCAL
            and self.backend is not GraphBackend.FALKORDB
        ):
            raise ValueError(
                f"backend={self.backend.value} cannot be started locally — "
                "the vendored runtime is FalkorDB. Use mode=remote."
            )
        return self
