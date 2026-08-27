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

from mailarc_ui.kit import card_heading, panel_card
from mailarc_ui.status.state import GraphRow, GraphStatusState


def _fact(label: str, value: rx.Var | str) -> rx.Component:
    return mn.data_list.item(
        mn.data_list.item_label(label),
        mn.data_list.item_value(value),
    )


def _status_header() -> rx.Component:
    return mn.group(
        mn.group(
            mn.badge(
                GraphStatusState.status_label,
                color=GraphStatusState.status_color,
                variant="light",
                size="lg",
            ),
            rx.cond(
                GraphStatusState.knn_supported,
                mn.badge("KNN ready", color="teal", variant="dot", size="lg"),
                mn.badge("KNN unavailable", color="gray", variant="dot", size="lg"),
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
            mn.button(
                "Refresh",
                on_click=GraphStatusState.refresh,
                loading=GraphStatusState.loading,
                variant="light",
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
                mn.alert(
                    GraphStatusState.error,
                    title="FalkorDB is not answering",
                    color="red",
                    variant="light",
                    icon=rx.icon("triangle-alert", size=16),
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
                mn.table(
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
                    striped=True,
                    highlight_on_hover=True,
                    tabular_nums=True,
                ),
                mn.empty_state(
                    icon=rx.icon("workflow", size=28),
                    title="No graphs yet",
                    description="Graphs appear here as soon as something writes one.",
                    align="center",
                ),
            ),
            gap="sm",
        ),
    )


def status_panel() -> rx.Component:
    """The whole page body: the three cards, stacked."""
    return mn.stack(
        connection_card(),
        metrics_card(),
        graphs_card(),
        gap="lg",
        w="100%",
    )
