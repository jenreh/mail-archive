"""The graph explorer: a corner of the archive, drawn.

The same four roles §6 fixes, and the split matters more here than elsewhere
because two of the halves fail in ways the other cannot see. ``model`` is the
projection — a :class:`~mailarc_analytics.Subgraph` in, cytoscape's elements,
stylesheet and layout out — and knows no I/O, no Reflex and no registry. Its
mistakes are silent in a browser: cytoscape refuses an element list with a
dangling edge and leaves the canvas blank rather than saying so.

``reads`` is the seam to everything outside — four services, all out of the
registry — and ``state`` is the page: the question, the picture and the
selection, with the reading pane and the tag actions mixed in rather than
written again.

The canvas itself is not here. It is ``kit.graph_canvas``, because the kit owns
every element this design has exactly one of, and there is exactly one canvas.
"""

from mailarc_ui.graph.components import explorer_panel
from mailarc_ui.graph.model import (
    FIT_PADDING,
    NODE_MAX_SIZE,
    NODE_MIN_SIZE,
    UNIFORM_WEIGHT,
    GraphView,
    LayoutName,
    NodeCard,
    SizeBy,
    card_of,
    elements_of,
    layout_of,
    stylesheet_of,
)
from mailarc_ui.graph.reads import (
    MEMBER_LIMIT,
    PICKER_LIMIT,
    cluster_members,
    graph_reader,
    picker_options,
    view_of,
)
from mailarc_ui.graph.state import (
    DARK_STYLESHEET,
    LIGHT_STYLESHEET,
    MAX_DEPTH,
    NOTHING_DERIVED,
    NOTHING_HERE,
    PICK_ONE,
    RECOMPUTED,
    RECOMPUTED_CIRCLE,
    GraphExplorerState,
)

__all__ = [
    "DARK_STYLESHEET",
    "FIT_PADDING",
    "LIGHT_STYLESHEET",
    "MAX_DEPTH",
    "MEMBER_LIMIT",
    "NODE_MAX_SIZE",
    "NODE_MIN_SIZE",
    "NOTHING_DERIVED",
    "NOTHING_HERE",
    "PICKER_LIMIT",
    "PICK_ONE",
    "RECOMPUTED",
    "RECOMPUTED_CIRCLE",
    "UNIFORM_WEIGHT",
    "GraphExplorerState",
    "GraphView",
    "LayoutName",
    "NodeCard",
    "SizeBy",
    "card_of",
    "cluster_members",
    "elements_of",
    "explorer_panel",
    "graph_reader",
    "layout_of",
    "picker_options",
    "stylesheet_of",
    "view_of",
]
