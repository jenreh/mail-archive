"""Reflex state for the FalkorDB status panel.

``mailarc_core``'s :class:`GraphServerStatus` is projected onto plain,
serialisable state vars here, so no component has to know the core's types.

The collaborator is looked up inside the handler that needs it and never at
import, which is the rule every state in this package follows: a module-level
lookup would run while ``app/app.py`` is still being imported — before the
composition root has published anything — and would turn a missing service
into an import error at startup instead of a sentence on one page.
"""

import asyncio
import logging

import reflex as rx
from appkit_commons.registry import service_registry
from pydantic import BaseModel
from reflex.event import EventCallback

from mailarc_core import GraphServerStatus
from mailarc_core.graph.health import GraphHealth

logger = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60


def graph_health() -> GraphHealth:
    """The health façade the composition root published.

    Call inside a method only. Same shape and same reason as
    ``mailarc_ui.insights.state.analytics_reader``: ``ServiceRegistry.get``
    raises a bare ``KeyError`` naming a registry nobody reading a status page
    has heard of, and the sentence that replaces it names the call that was
    missed.
    """
    try:
        return service_registry().get(GraphHealth)
    except KeyError as error:
        raise RuntimeError(
            "No graph health is registered — did app.composition run?"
        ) from error


class GraphRow(BaseModel):
    """One row of the graph inventory table.

    A pydantic model, not `rx.Base` — that was removed in Reflex 0.9. Reflex
    serialises `BaseModel` and resolves attribute access on it inside
    `rx.foreach`, so `row.name` compiles to the right frontend expression.
    """

    name: str
    nodes: str
    edges: str


class GraphStatusState(rx.State):
    """Live view of the graph server behind the status page."""

    checked: bool = False
    loading: bool = False
    polling: bool = False
    poll_interval: int = 5

    reachable: bool = False
    mode: str = ""
    endpoint: str = ""
    error: str = ""
    checked_at: str = ""

    redis_version: str = ""
    falkordb_version: str = ""
    knn_supported: bool = False
    latency: str = ""

    uptime: str = ""
    used_memory: str = ""
    connected_clients: str = ""
    commands_processed: str = ""

    graphs: list[GraphRow] = []

    @rx.var
    def status_label(self) -> str:
        if not self.checked:
            return "Checking…"
        return "Connected" if self.reachable else "Unreachable"

    @rx.var
    def status_color(self) -> str:
        if not self.checked:
            return "gray"
        return "green" if self.reachable else "red"

    @rx.var
    def has_graphs(self) -> bool:
        return len(self.graphs) > 0

    @rx.event
    async def refresh(self) -> None:
        """Read the server once, on demand."""
        self.loading = True
        try:
            health = graph_health()
            self._apply(await health.status(), health)
        finally:
            self.loading = False

    @rx.event
    async def start_polling(self) -> EventCallback[()] | None:
        """Kick off background polling unless it is already running.

        The page's ``on_load``. Answering with no follow-up event leaves the
        panel in the state it opens in: "Checking…" and nothing read.
        """
        if self.polling:
            return None
        self.polling = True
        return GraphStatusState.poll

    @rx.event
    def stop_polling(self) -> None:
        self.polling = False

    @rx.event(background=True)
    async def poll(self) -> None:
        """Keep the panel live while the page is open.

        The lock is held only around the state mutation; the sleep happens
        outside it so the rest of the app is never blocked waiting on us.
        """
        while True:
            async with self:
                if not self.polling:
                    return
            reading: tuple[GraphServerStatus, GraphHealth] | None = None
            try:
                health = graph_health()
                reading = (await health.status(), health)
            except Exception:
                logger.exception("Graph status poll failed")
            async with self:
                if not self.polling:
                    return
                if reading is not None:
                    self._apply(*reading)
            await asyncio.sleep(self.poll_interval)

    def _apply(self, status: GraphServerStatus, health: GraphHealth) -> None:
        self.checked = True
        self.reachable = status.reachable
        self.mode = str(status.mode)
        self.endpoint = status.endpoint
        # A failed start ("run `task tauri:vendor`") explains far more than the
        # symptom it produces ("connection refused"), so prefer it. Asked only
        # when the server is down: it is a read through to the server handle,
        # and a reachable server has nothing to explain.
        self.error = (
            "" if status.reachable else (health.startup_error() or status.error or "")
        )
        # The domain keeps UTC; a human reading the panel wants their own clock.
        self.checked_at = status.checked_at.astimezone().strftime("%H:%M:%S")

        self.redis_version = status.redis_version or "—"
        self.falkordb_version = status.falkordb_version or "—"
        self.knn_supported = status.vector_knn_supported
        self.latency = (
            f"{status.latency_ms:.1f} ms" if status.latency_ms is not None else "—"
        )

        metrics = status.metrics
        self.uptime = format_uptime(metrics.uptime_seconds) if metrics else "—"
        self.used_memory = metrics.used_memory_human if metrics else "—"
        self.connected_clients = str(metrics.connected_clients) if metrics else "—"
        self.commands_processed = (
            f"{metrics.total_commands_processed:,}" if metrics else "—"
        )

        self.graphs = [
            GraphRow(
                name=graph.name,
                nodes=_count(graph.node_count),
                edges=_count(graph.edge_count),
            )
            for graph in status.graphs
        ]


def format_uptime(seconds: int) -> str:
    """Render an uptime in the largest unit that still reads naturally."""
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds}s"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds // _SECONDS_PER_MINUTE}m {seconds % _SECONDS_PER_MINUTE}s"
    hours, remainder = divmod(seconds, _SECONDS_PER_HOUR)
    return f"{hours}h {remainder // _SECONDS_PER_MINUTE}m"


def _count(value: int) -> str:
    """``-1`` marks a graph the adapter could not read."""
    return "?" if value < 0 else f"{value:,}"
