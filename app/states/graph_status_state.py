"""Reflex state for the FalkorDB status panel.

``mailarc_core``'s :class:`GraphServerStatus` is projected onto plain,
serialisable state vars here, so no component has to know the core's types.
"""

import asyncio
import logging

import reflex as rx
from pydantic import BaseModel
from reflex.event import EventCallback

from app.composition import graph_startup_error, graph_status
from mailarc_core import GraphServerStatus

logger = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60


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
    """Live view of the graph server behind the Hello World page."""

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
            status = await graph_status()
            self._apply(status)
        finally:
            self.loading = False

    @rx.event
    async def start_polling(self) -> EventCallback[()] | None:
        """Kick off background polling unless it is already running."""
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
            try:
                status = await graph_status()
            except Exception:
                logger.exception("Graph status poll failed")
                status = None
            async with self:
                if not self.polling:
                    return
                if status is not None:
                    self._apply(status)
            await asyncio.sleep(self.poll_interval)

    def _apply(self, status: GraphServerStatus) -> None:
        self.checked = True
        self.reachable = status.reachable
        self.mode = str(status.mode)
        self.endpoint = status.endpoint
        # A failed start ("run `task tauri:vendor`") explains far more than the
        # symptom it produces ("connection refused"), so prefer it.
        self.error = (
            "" if status.reachable else (graph_startup_error() or status.error or "")
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
