"""The explorer's three columns: what to draw, the drawing, and what is picked.

Laid out the way the search page is — panes in an ``mn.splitter``, each a
``column_card`` with an edge of its own and canvas visible between them, the
resizer *being* the gap. The difference is that all three are in the splitter
here rather than two: on the search page the form is a fixed 320px of boxes,
and here the middle column is a canvas whose usable size is the whole point, so
a person has to be able to take room from either side of it.

Nothing in this module decides anything. The elements, the stylesheet and the
layout are computed in :mod:`mailarc_ui.graph.model` and held by
:mod:`mailarc_ui.graph.state`; every design element is a
:mod:`mailarc_ui.kit` function. The one thing this file does own is which of the
two stylesheets a reader gets, because that is the one fact the state cannot
learn: cytoscape paints into a ``<canvas>``, no custom property is resolvable
from inside it, and which colour scheme is in force lives in the browser.

The kind toggles double as the legend. A dot in the kind's own colour beside
the name it hides is what ``.ma-graph-dot`` was written for, and a legend that
is not also a control would be a second row saying the same six things.
"""

from __future__ import annotations

from typing import cast

import appkit_mantine as mn
import reflex as rx

from mailarc_analytics import NodeKind
from mailarc_ui.graph.model import GraphView, LayoutName, NodeCard, SizeBy
from mailarc_ui.graph.state import (
    DARK_STYLESHEET,
    LIGHT_STYLESHEET,
    MAX_DEPTH,
    GraphExplorerState,
)
from mailarc_ui.kit import (
    COLUMN_GAP,
    card_heading,
    column_card,
    empty_panel,
    graph_canvas,
    message,
    number_field,
    panel_card,
    pill_action,
    scroll_table,
    segmented_field,
    select_field,
    soft_button,
    spinner,
)
from mailarc_ui.message_detail import message_tabs
from mailarc_ui.tags.components import promote_form, suggestion_rows, tag_chips

CONTROL_WIDTH = "280px"
"""What the left column opens at. Wide enough for a topic's label in the
picker and narrow enough that the canvas is still the page."""

DETAIL_WIDTH = "380px"
"""And the right one — a reading pane's width, because on a message that is
exactly what it is."""

VIEW_OPTIONS: list[dict[str, str]] = [
    {"value": GraphView.OVERVIEW.value, "label": "Map"},
    {"value": GraphView.TOPIC.value, "label": "Topic"},
    {"value": GraphView.COMMUNITY.value, "label": "Circle"},
    {"value": GraphView.TAG.value, "label": "Tag"},
    {"value": GraphView.ADDRESS.value, "label": "Person"},
    {"value": GraphView.MESSAGE.value, "label": "Mail"},
]
"""The six views, in the words the page uses for them.

``Map``, ``Circle``, ``Person`` and ``Mail`` rather than the enum's own names:
what a reader picks between is what they are looking for, and "community" is
the algorithm's word for it rather than theirs.
"""

SIZE_OPTIONS: list[dict[str, str]] = [
    {"value": SizeBy.UNIFORM.value, "label": "Nothing"},
    {"value": SizeBy.DEGREE.value, "label": "Connections here"},
    {"value": SizeBy.PAGERANK.value, "label": "Reply centrality"},
    {"value": SizeBy.IMPORTANCE.value, "label": "Importance"},
    {"value": SizeBy.COUNT.value, "label": "How much it holds"},
]
"""What a node's diameter may mean. ``Nothing`` is first and is the default:
before a rebuild has scored anything, "do not make a claim about size" is the
only honest answer."""

LAYOUT_OPTIONS: list[dict[str, str]] = [
    {"value": LayoutName.COSE.value, "label": "Force"},
    {"value": LayoutName.CONCENTRIC.value, "label": "Concentric"},
    {"value": LayoutName.BREADTHFIRST.value, "label": "Tree"},
]

KINDS: tuple[NodeKind, ...] = tuple(NodeKind)
"""Every kind that can be on the canvas, for the legend that hides them."""


def explorer_panel() -> rx.Component:
    """The whole page's body: controls, canvas, details."""
    return mn.splitter(
        mn.splitter.pane(
            column_card(_controls(), padding="md", style={"overflow": "auto"}),
            default_size=CONTROL_WIDTH,
            min="220px",
            max="420px",
            style={"overflow": "hidden"},
        ),
        mn.splitter.pane(
            column_card(_canvas(), padding="md", style={"minWidth": 0}),
            # A number rather than a length: Mantine reads a px string as a
            # fixed pane and a number as a share of what is left, so the two
            # sides keep their widths and the canvas absorbs the window.
            default_size=1,
            style={"overflow": "hidden", "minWidth": 0},
        ),
        mn.splitter.pane(
            column_card(_details(), padding="md", style={"minWidth": 0}),
            default_size=DETAIL_WIDTH,
            min="300px",
            max="620px",
            style={"overflow": "hidden"},
        ),
        line_size=COLUMN_GAP,
        handle_color="transparent",
        with_handle=True,
        h="100%",
        w="100%",
    )


def _controls() -> rx.Component:
    """What is drawn, and how."""
    return mn.stack(
        card_heading("compass", "What to look at"),
        segmented_field(
            "View",
            data=VIEW_OPTIONS,
            value=GraphExplorerState.view_name,
            on_change=GraphExplorerState.choose_view,
            orientation="vertical",
            size="xs",
        ),
        rx.cond(GraphExplorerState.needs_a_root, _root_picker(), rx.fragment()),
        rx.cond(
            GraphExplorerState.view_name == GraphView.MESSAGE.value,
            _depth_field(),
            rx.fragment(),
        ),
        mn.divider(),
        card_heading("circle-dot", "How to draw it"),
        select_field(
            "Size by",
            description="A node with no such number is drawn in the middle.",
            data=SIZE_OPTIONS,
            value=GraphExplorerState.size_by,
            on_change=GraphExplorerState.set_size_by,
            allow_deselect=False,
            size="xs",
        ),
        select_field(
            "Arrangement",
            data=LAYOUT_OPTIONS,
            value=GraphExplorerState.layout_name,
            on_change=GraphExplorerState.set_layout,
            allow_deselect=False,
            size="xs",
        ),
        _legend(),
        soft_button(
            "Fit",
            left_section=rx.icon("maximize", size=14),
            on_click=GraphExplorerState.fit,
            size="xs",
        ),
        gap="sm",
        w="100%",
    )


def _root_picker() -> rx.Component:
    """Which topic, circle, tag, person or mail the picture is rooted at."""
    return select_field(
        "Rooted at",
        data=GraphExplorerState.options,
        value=GraphExplorerState.picked_id,
        on_change=GraphExplorerState.pick,
        placeholder="Pick one",
        searchable=True,
        nothing_found_message="Nothing to pick — has a rebuild run?",
        size="xs",
    )


def _depth_field() -> rx.Component:
    return number_field(
        "Reply depth",
        description="How far to follow the answers around this mail.",
        value=GraphExplorerState.depth,
        on_change=GraphExplorerState.set_depth,
        min=1,
        max=MAX_DEPTH,
        size="xs",
    )


def _legend() -> rx.Component:
    """The six kinds, coloured — and each one a switch that hides its own."""
    return mn.stack(
        mn.text("Show", class_name="ma-field-label"),
        mn.group(
            *[_kind_toggle(kind) for kind in KINDS],
            gap=4,
            wrap="wrap",
        ),
        gap=8,
    )


def _kind_toggle(kind: NodeKind) -> rx.Component:
    """One kind's dot and name, pressed to take it off the canvas."""
    return pill_action(
        kind.value,
        left_section=mn.box(
            class_name="ma-graph-dot",
            custom_attrs={"data-kind": kind.value},
        ),
        on_click=GraphExplorerState.toggle_kind(kind.value),
        custom_attrs={
            # `contains` is a Var operation and ty reads the class attribute as
            # the `list[str]` its annotation declares, which is what the state
            # holds rather than what the class exposes.
            "data-hidden": GraphExplorerState.hidden_kinds.contains(  # ty: ignore[unresolved-attribute]
                kind.value
            ),
        },
    )


def _canvas() -> rx.Component:
    """The picture, and the one line that says why there is none."""
    return mn.stack(
        rx.cond(
            GraphExplorerState.error != "",
            message(GraphExplorerState.error, "failure"),
            rx.fragment(),
        ),
        rx.cond(
            GraphExplorerState.notice != "",
            message(GraphExplorerState.notice, "note"),
            rx.fragment(),
        ),
        rx.cond(
            GraphExplorerState.truncated_notice != "",
            message(GraphExplorerState.truncated_notice, "warning"),
            rx.fragment(),
        ),
        rx.cond(GraphExplorerState.loading, spinner(), rx.fragment()),
        graph_canvas(
            elements=GraphExplorerState.elements,
            # The one decision this module makes: which palette the canvas is
            # painted in. Neither the state nor the projection can, because a
            # colour scheme is a fact about the browser.
            stylesheet=rx.color_mode_cond(light=LIGHT_STYLESHEET, dark=DARK_STYLESHEET),
            layout=GraphExplorerState.layout,
            selected=GraphExplorerState.selected_node.id,
            fit_token=GraphExplorerState.fit_token,
            on_select=GraphExplorerState.select_node,
            on_expand=GraphExplorerState.expand_node,
            on_background=GraphExplorerState.clear_selection,
        ),
        mn.text(
            "Tap a node to open it, double-tap to add one hop around it.",
            size="xs",
            c="dimmed",
        ),
        gap="xs",
        w="100%",
    )


def _details() -> rx.Component:
    """Whatever is picked, in the terms its kind deserves.

    ``rx.match`` is typed as returning a component *or* a Var — it is both,
    depending on what its cases are — and every case here is a component.
    """
    return cast(
        rx.Component,
        rx.match(
            GraphExplorerState.selected_node.kind,
            (NodeKind.MESSAGE.value, _message_details()),
            (NodeKind.TOPIC.value, _cluster_details()),
            (NodeKind.COMMUNITY.value, _cluster_details()),
            (NodeKind.TAG.value, _tag_details()),
            (NodeKind.ADDRESS.value, _address_details()),
            (NodeKind.THREAD.value, _node_card()),
            _nothing_picked(),
        ),
    )


def _nothing_picked() -> rx.Component:
    return empty_panel(
        "mouse-pointer-click",
        "Nothing picked",
        "Tap a node on the canvas and what it is shows up here.",
    )


def _node_card() -> rx.Component:
    """What the store says about the picked node, one line per property."""
    return mn.stack(
        mn.text(GraphExplorerState.selected_node.title, size="sm", fw=600),
        rx.foreach(
            GraphExplorerState.selected_node.lines,
            lambda line: mn.text(line, size="xs", c="dimmed"),
        ),
        gap=4,
        w="100%",
    )


def _message_details() -> rx.Component:
    """The reading pane, and the tags this message wears."""
    return mn.stack(
        tag_chips(GraphExplorerState, GraphExplorerState.selected_node.id),
        message_tabs(GraphExplorerState),
        gap="xs",
        w="100%",
        style={"minHeight": 0, "flex": "1 1 auto"},
    )


def _cluster_details() -> rx.Component:
    """A topic or a circle: what of it is drawn, and the way to make it a tag."""
    return mn.stack(
        _node_card(),
        panel_card(
            mn.stack(
                mn.text("Make this a project", size="sm", fw=600),
                promote_form(
                    GraphExplorerState,
                    GraphExplorerState.selected_node.kind,
                    GraphExplorerState.selected_node.id,
                ),
                gap="xs",
            ),
        ),
        _members_table(),
        gap="sm",
        w="100%",
    )


def _members_table() -> rx.Component:
    """The cluster's mail *as drawn*.

    Deliberately what is on the canvas rather than the whole cluster: the
    picture is capped, and a table that quietly listed more than the drawing
    would be two answers to one question. Promoting takes the whole cluster,
    which is what the sentence under the form says.
    """
    return rx.cond(
        GraphExplorerState.cluster_rows,
        scroll_table(
            mn.table.thead(mn.table.tr(mn.table.th("On the canvas"))),
            mn.table.tbody(
                rx.foreach(GraphExplorerState.cluster_rows, _member_row),
            ),
            rows=8,
        ),
        rx.fragment(),
    )


def _member_row(row: NodeCard) -> rx.Component:
    return mn.table.tr(
        mn.table.td(
            mn.text(row.title, size="xs", line_clamp=1),
            on_click=GraphExplorerState.select_node(row.id),
            style={"cursor": "pointer"},
        ),
    )


def _tag_details() -> rx.Component:
    """A tag: what it is, and what an analysis thinks it is missing."""
    return mn.stack(
        _node_card(),
        suggestion_rows(GraphExplorerState),
        gap="sm",
        w="100%",
    )


def _address_details() -> rx.Component:
    """A correspondent, and the route between them and the root of the view."""
    return mn.stack(
        _node_card(),
        rx.cond(
            GraphExplorerState.picked_id != "",
            pill_action(
                "Route from the root",
                icon="route",
                on_click=GraphExplorerState.show_path(
                    GraphExplorerState.selected_node.id
                ),
            ),
            rx.fragment(),
        ),
        gap="sm",
        w="100%",
    )
