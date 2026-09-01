"""The three cards the graph status page is made of.

Moved out of ``app/pages/home.py``, where they were the body of a Hello World
page. What changed on the way is the card: all three open-coded the same
``mn.card(shadow="sm", …)`` the rest of the application open-coded six more
times, and they now go through :func:`~mailarc_ui.kit.panel_card` like every
other surface in the archive.

Connection, server, inventory — in that order, because that is the order the
questions are asked in. Is it answering at all; if it is, how is it holding up;
and only then, what is in it.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import (
    card_heading,
    dot_badge,
    empty_panel,
    message,
    panel_card,
    scroll_table,
    soft_button,
    status_badge,
)
from mailarc_ui.status.state import GraphRow, GraphStatusState


def _fact(label: str, value: rx.Var | str) -> rx.Component:
    return mn.data_list.item(
        mn.data_list.item_label(label),
        mn.data_list.item_value(value),
    )


def _status_header() -> rx.Component:
    return mn.group(
        mn.group(
            status_badge(
                GraphStatusState.status_label,
                GraphStatusState.status_color,
                size="lg",
            ),
            rx.cond(
                GraphStatusState.knn_supported,
                dot_badge("KNN ready", "teal"),
                dot_badge("KNN unavailable", "gray"),
            ),
            gap="xs",
        ),
        mn.group(
            rx.cond(
                GraphStatusState.checked_at != "",
                mn.text(
                    f"checked {GraphStatusState.checked_at}", size="xs", c="dimmed"
                ),
                mn.text(""),
            ),
            soft_button(
                "Refresh",
                on_click=GraphStatusState.refresh,
                loading=GraphStatusState.loading,
                size="xs",
                left_section=rx.icon("refresh-cw", size=14),
            ),
            gap="sm",
        ),
        justify="space-between",
        align="center",
        w="100%",
    )


def connection_card() -> rx.Component:
    """Whether the server is answering, and what it says it is."""
    return panel_card(
        mn.stack(
            _status_header(),
            rx.cond(
                GraphStatusState.error != "",
                message(
                    GraphStatusState.error,
                    "failure",
                    title="FalkorDB is not answering",
                ),
                mn.text(""),
            ),
            mn.data_list(
                _fact("Mode", GraphStatusState.mode),
                _fact("Endpoint", GraphStatusState.endpoint),
                _fact("Redis", GraphStatusState.redis_version),
                _fact("FalkorDB", GraphStatusState.falkordb_version),
                _fact("Latency", GraphStatusState.latency),
                orientation="horizontal",
                label_width=110,
                with_divider=True,
                size="sm",
            ),
            gap="md",
        ),
    )


def metrics_card() -> rx.Component:
    """How the process is holding up: uptime, memory, clients, commands."""
    return panel_card(
        mn.stack(
            card_heading("activity", "Server"),
            mn.data_list(
                _fact("Uptime", GraphStatusState.uptime),
                _fact("Memory", GraphStatusState.used_memory),
                _fact("Clients", GraphStatusState.connected_clients),
                _fact("Commands", GraphStatusState.commands_processed),
                orientation="horizontal",
                label_width=110,
                size="sm",
            ),
            gap="sm",
        ),
    )


def _graph_row(row: GraphRow) -> rx.Component:
    return mn.table.tr(
        mn.table.td(row.name),
        mn.table.td(row.nodes),
        mn.table.td(row.edges),
    )


def graphs_card() -> rx.Component:
    """What the server holds, one row per graph."""
    return panel_card(
        mn.stack(
            card_heading("workflow", "Graphs"),
            rx.cond(
                GraphStatusState.has_graphs,
                scroll_table(
                    mn.table.thead(
                        mn.table.tr(
                            mn.table.th("Name"),
                            mn.table.th("Nodes"),
                            mn.table.th("Edges"),
                        ),
                    ),
                    mn.table.tbody(
                        rx.foreach(GraphStatusState.graphs, _graph_row),
                    ),
                ),
                empty_panel(
                    "workflow",
                    "No graphs yet",
                    "Graphs appear here as soon as something writes one.",
                ),
            ),
            gap="sm",
        ),
    )


def status_panel() -> rx.Component:
    """The whole page body: the connection across the top, the other two beside
    each other under it.

    Still connection, server, inventory — the order the questions are asked in
    — but the page fills the window now, and three cards stacked down it is a
    four-fact list and a three-column table each drawn across 1300px. The one
    that earns the full width is the connection: it carries the status badge,
    the refresh control and five facts, and it is what a reader looks at first.

    Under it the split follows the content: the server is four labelled
    numbers and takes the narrow side, the inventory is a table and takes the
    wide one.
    """
    return mn.stack(
        connection_card(),
        mn.grid(
            mn.grid_col(metrics_card(), span={"base": 12, "lg": 4}),
            mn.grid_col(graphs_card(), span={"base": 12, "lg": 8}),
            gutter="lg",
            w="100%",
        ),
        gap="lg",
        w="100%",
    )
