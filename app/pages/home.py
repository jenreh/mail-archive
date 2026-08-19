"""Hello World plus the live state of the local FalkorDB."""

import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.templates import navbar_layout

from app.components.navbar import app_navbar
from app.states.graph_status_state import GraphRow, GraphStatusState


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


def _connection_card() -> rx.Component:
    return mn.card(
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
        shadow="sm",
        padding="lg",
        radius="md",
        with_border=True,
        w="100%",
    )


def _metrics_card() -> rx.Component:
    return mn.card(
        mn.stack(
            mn.text("Server", fw=600, size="sm"),
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
        shadow="sm",
        padding="lg",
        radius="md",
        with_border=True,
        w="100%",
    )


def _graph_row(row: GraphRow) -> rx.Component:
    return mn.table.tr(
        mn.table.td(row.name),
        mn.table.td(row.nodes),
        mn.table.td(row.edges),
    )


def _graphs_card() -> rx.Component:
    return mn.card(
        mn.stack(
            mn.text("Graphs", fw=600, size="sm"),
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
        shadow="sm",
        padding="lg",
        radius="md",
        with_border=True,
        w="100%",
    )


@navbar_layout(
    route="/",
    title="mail-archive",
    description="Hello World and the live state of the local FalkorDB",
    navbar=app_navbar(),
    with_header=False,
    # ty cannot model reflex event-handler calls; suppress the false positive.
    on_load=[GraphStatusState.start_polling],  # ty: ignore[invalid-argument-type]
)
def home_page() -> rx.Component:
    return mn.stack(
        mn.stack(
            mn.title("Hello World", order=1),
            mn.text(
                "The desktop app runs this page and its FalkorDB from binaries "
                "bundled inside the app — nothing is installed on the machine.",
                c="dimmed",
                size="sm",
            ),
            gap="xs",
        ),
        _connection_card(),
        _metrics_card(),
        _graphs_card(),
        gap="lg",
        w="100%",
        maw=900,
        mx="auto",
        p="2rem",
    )
