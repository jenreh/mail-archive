"""A subgraph, turned into the three things a cytoscape canvas is made of.

Elements, a stylesheet and a layout. Nothing here reads anything, holds a
session or knows a Reflex ``State`` — a :class:`~mailarc_analytics.Subgraph`
goes in and plain dictionaries come out — which is the point of the split: the
explorer's state is wrong when a read is left spinning or a query parameter is
misread, and this module is wrong when a node is sized off a number it does not
carry or an edge is drawn to a node that was hidden. The second kind is
invisible in a browser (cytoscape refuses the whole ``add`` and leaves the
canvas blank) and trivial here.

Three decisions worth knowing before changing anything:

**A node's colour rides with the node, not with a selector.** The stylesheet
says ``background-color: data(color)`` once, and :func:`elements_of` puts the
hex on every element. That is what lets one stylesheet serve both colour
schemes: the page hands in :attr:`~mailarc_ui.theme.Palette.DARK` and every
circle changes without a rule being rewritten.

**A missing weight is not a weight of zero.** ``GraphNode.weights`` is sparse
on purpose — a thread has no importance, an address has no count — so a node
that does not carry the number the page is sizing by is drawn at
:data:`UNIFORM_WEIGHT`, in the middle, rather than as the smallest circle on
screen. The smallest circle is a claim, and it would be a false one.

**Hiding a kind hides its edges too.** Cytoscape throws on an edge whose source
or target is not in the collection, and a throw inside ``cy.add`` aborts the
whole batch, so a single filtered-out node would empty the canvas.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict

from mailarc_analytics import GraphEdge, GraphNode, Subgraph
from mailarc_ui.theme import Palette

logger = logging.getLogger(__name__)

NODE_MIN_SIZE = 14
"""The diameter of a node carrying the smallest number in its picture, in px.

Small enough that a hub stands out beside it and large enough to still be a
click target; below about twelve a node is a dot nobody can hit.
"""

NODE_MAX_SIZE = 52
"""The diameter of the heaviest node. Four times the smallest, which is as far
apart as two circles can be while both still read as the same kind of thing."""

UNIFORM_WEIGHT = 0.5
"""What a node is sized at when there is nothing to size it by.

Both cases land here: ``size_by=uniform``, where the page is deliberately not
making a claim, and a node missing the chosen number, where the archive has
nothing to say. Deliberately the middle of the scale rather than either end.
"""

LABEL_DENSITY_LIMIT = 40
"""Nodes above which the canvas stops naming all of them.

Measured on a real archive rather than chosen: seventy-five topics at the
default fit-zoom wrote their labels over each other into a smear, which loses
every name instead of the least interesting ones. Forty is where a label of
:data:`LABEL_WIDTH` still has room beside its neighbour in the middle column.
"""

LABELLED_MAX = 24
"""Names kept when a picture is over :data:`LABEL_DENSITY_LIMIT`.

The heaviest, because the whole reason to read a crowded map is to find what is
big in it. The rest keep their node, their size and their click — only the text
goes, and the details column still names whatever is tapped.
"""

LABEL_WIDTH = 96
"""Pixels a label may run to before it is ellipsised.

Narrower than a node is wide at :data:`NODE_MAX_SIZE`, so two adjacent labels
collide only when their nodes already overlap.
"""


FIT_PADDING = 24
"""How much room ``cy.fit()`` leaves around the picture, in px. Stated here
because a layout's own ``padding`` has to agree with it or the canvas jumps
between the layout settling and the fit."""


class GraphView(StrEnum):
    """What the explorer is rooted at — the ``?view=`` a link carries.

    One per :meth:`~mailarc_analytics.queries.graphs.GraphReader` method that
    takes a root, plus ``overview``, which takes none. Lower case for the
    reason :class:`~mailarc_analytics.NodeKind` is: these reach a URL a person
    reads, and ``/graph?view=topic&id=…`` is worth more than an exact echo of a
    label.

    There is no ``thread`` view: a thread is drawn as the hub its messages hang
    off and has no read of its own (see ``GraphReader.expand``).
    """

    OVERVIEW = "overview"
    MESSAGE = "message"
    ADDRESS = "address"
    TOPIC = "topic"
    TAG = "tag"
    COMMUNITY = "community"


class SizeBy(StrEnum):
    """The number the canvas draws a node's diameter from.

    :class:`~mailarc_analytics.Weight` plus ``uniform``, and the values match
    exactly — the reader normalises its weights under those keys and a spelling
    that disagreed would be a dropdown entry that silently sizes nothing.
    ``uniform`` is the extra one: "do not make a claim about size", which is the
    honest default before a rebuild has scored anything.
    """

    UNIFORM = "uniform"
    DEGREE = "degree"
    PAGERANK = "pagerank"
    IMPORTANCE = "importance"
    COUNT = "count"


class LayoutName(StrEnum):
    """The three layouts cytoscape ships that suit these pictures.

    ``cose`` is the default and the fallback: a force layout is the only one
    that reads well on a graph nobody has told anything about. ``concentric``
    puts the heaviest node in the middle, which is the ego views. ``breadthfirst``
    is for a reply chain, where the direction is the story.

    ``fcose`` is deliberately absent. It is a separate npm package that has to
    be registered on the cytoscape instance, and one extra dependency is not
    worth a fourth arrangement of the same nodes.
    """

    COSE = "cose"
    CONCENTRIC = "concentric"
    BREADTHFIRST = "breadthfirst"


class NodeCard(BaseModel):
    """The picked node, as the details column prints it.

    Constructed empty as the "nothing is selected" sentinel, so no component in
    that column has to guard on ``None`` — the same argument
    :data:`mailarc_ui.insights.model.NO_TOTALS` makes.

    :attr:`kind` is a plain string rather than a
    :class:`~mailarc_analytics.NodeKind`, because what reads it is an
    ``rx.match`` in the browser.
    """

    model_config = ConfigDict(frozen=True)

    id: str = ""
    kind: str = ""
    title: str = ""
    lines: tuple[str, ...] = ()


_VIEW_DEFAULTS: Mapping[str, tuple[SizeBy, LayoutName]] = MappingProxyType(
    {GraphView.OVERVIEW.value: (SizeBy.COUNT, LayoutName.CONCENTRIC)}
)
"""How a view wants to be drawn before anybody touches the controls.

Only the map differs, and it differs for two reasons that are both about what
the overview *is* rather than about taste.

It sizes by ``count`` because ``message_count`` is written by every rebuild
there has ever been — unlike ``importance``, which is null until this feature's
stages have run once. So ``uniform`` on the map is not the honest default it is
on a message view; it is a picture that throws away the one number it is
certain to have, and draws an archive of one 88-message topic and forty
two-message ones as identical circles.

It is laid out concentrically because the map has **no edge between two
topics**: collections are joined only through circles and tags, so an archive
with neither hands ``cose`` a set with no forces in it, and a force layout with
nothing to pull against falls back to a lattice. Concentric ranks by weight
instead, which is the question the map is asked.
"""


def defaults_for(view: GraphView | str) -> tuple[SizeBy, LayoutName]:
    """What *view* is drawn with until somebody chooses otherwise.

    Falls through to the rooted-view pair for anything unknown, so a ``?view=``
    somebody bookmarked before a view was renamed still draws.
    """
    key = view.value if isinstance(view, GraphView) else str(view)
    return _VIEW_DEFAULTS.get(key, (SizeBy.UNIFORM, LayoutName.COSE))


def elements_of(
    subgraph: Subgraph,
    *,
    size_by: SizeBy = SizeBy.UNIFORM,
    hidden_kinds: Sequence[str] = (),
    palette: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """*subgraph* as the element list ``cy.add`` takes, nodes first.

    Every node's colour and diameter is decided here rather than by a selector,
    so the same stylesheet serves both colour schemes and every value the canvas
    needs is in the element that carries it.

    A kind named in *hidden_kinds* loses its nodes **and every edge that touched
    one** — see the module docstring for why that is not merely tidy.
    """
    colours = Palette.LIGHT if palette is None else palette
    hidden = frozenset(hidden_kinds)
    nodes = [one for one in subgraph.nodes if one.kind.value not in hidden]
    drawn = {one.id for one in nodes}
    named = _named(nodes, size_by)
    return [
        _node_element(one, size_by, colours, labelled=one.id in named) for one in nodes
    ] + [
        _edge_element(one)
        for one in subgraph.edges
        if one.source in drawn and one.target in drawn
    ]


def stylesheet_of(palette: Mapping[str, str]) -> list[dict[str, Any]]:
    """The cytoscape stylesheet, with *palette*'s hexes already in it.

    Four rules and no per-kind selector: the fill comes off the element
    (``data(color)``) and the diameter off its weight, so this is the same
    stylesheet whatever is on the canvas.
    """
    size = f"mapData(weight, 0, 1, {NODE_MIN_SIZE}, {NODE_MAX_SIZE})"
    return [
        {
            "selector": "node",
            "style": {
                "width": size,
                "height": size,
                "background-color": "data(color)",
                "border-width": 1.5,
                "border-color": palette["surface"],
                "label": "data(label)",
                "color": palette["text"],
                "font-size": 10,
                "text-valign": "bottom",
                "text-margin-y": 4,
                "text-wrap": "ellipsis",
                "text-max-width": LABEL_WIDTH,
                # Below this the labels are unreadable anyway, and drawing a
                # few hundred of them is what makes a zoomed-out canvas stutter.
                "min-zoomed-font-size": 8,
            },
        },
        {
            "selector": "node:selected",
            "style": {
                "border-width": 3,
                "border-color": palette["selected"],
            },
        },
        {
            "selector": "edge",
            "style": {
                "width": 1,
                "line-color": palette["edge"],
                "target-arrow-color": palette["edge"],
                "target-arrow-shape": "triangle",
                "arrow-scale": 0.7,
                "curve-style": "bezier",
                "opacity": 0.75,
            },
        },
        {
            "selector": "edge:selected",
            "style": {
                "line-color": palette["selected"],
                "target-arrow-color": palette["selected"],
                "opacity": 1,
            },
        },
    ]


def layout_of(name: LayoutName | str) -> dict[str, Any]:
    """The options for *name*, or :attr:`LayoutName.COSE` if it is not one.

    A name arrives from a query parameter and from a link somebody bookmarked
    before a layout was renamed, so an unknown one is a picture drawn the
    default way rather than a page that fails to load.

    Nothing animates and the force layout does not randomise: two runs over the
    same subgraph have to draw the same picture, or a reader cannot tell a
    changed archive from a re-run layout.
    """
    common: dict[str, Any] = {"animate": False, "fit": True, "padding": FIT_PADDING}
    if name == LayoutName.CONCENTRIC:
        return {**common, "name": "concentric", "minNodeSpacing": 24}
    if name == LayoutName.BREADTHFIRST:
        return {**common, "name": "breadthfirst", "directed": True, "spacingFactor": 1}
    if name != LayoutName.COSE:
        logger.debug("Unknown layout %r; drawing with %s", name, LayoutName.COSE.value)
    return {
        **common,
        "name": "cose",
        "randomize": False,
        "componentSpacing": 60,
        "nodeOverlap": 12,
        "idealEdgeLength": 80,
    }


def card_of(node: GraphNode) -> NodeCard:
    """*node* as the details column prints it — a title and its plain lines.

    ``GraphNode.props`` is already text (the reader renders a timestamp once,
    where it knows the zone), so all that is left is an order. Sorted by key,
    because the props a node carries differ by kind and a card whose lines moved
    about between two clicks is a card nobody reads twice.
    """
    return NodeCard(
        id=node.id,
        kind=node.kind.value,
        title=node.label or node.id,
        lines=tuple(
            f"{name.replace('_', ' ')}: {value}"
            for name, value in sorted(node.props.items())
        ),
    )


def _named(nodes: Sequence[GraphNode], size_by: SizeBy) -> frozenset[str]:
    """Which of *nodes* keep their label — all of them, until it is a smear.

    Over :data:`LABEL_DENSITY_LIMIT` the canvas writes its names over each
    other, and an unreadable name is worth less than none: the reader loses
    every label rather than the ones they were never going to read. So the
    heaviest :data:`LABELLED_MAX` keep theirs and the tail is drawn bare.

    Ordered by weight and then by id, never by arrival: the picture must not
    depend on the order a statement happened to return its rows in. Where the
    weights are all equal — ``size_by=uniform`` on a crowded canvas — that
    leaves the id as the whole of the order, which is arbitrary but stable, and
    still better than a smear.
    """
    if len(nodes) <= LABEL_DENSITY_LIMIT:
        return frozenset(one.id for one in nodes)
    ranked = sorted(nodes, key=lambda one: (-_weight_of(one, size_by), one.id))
    return frozenset(one.id for one in ranked[:LABELLED_MAX])


def _node_element(
    node: GraphNode,
    size_by: SizeBy,
    colours: Mapping[str, str],
    *,
    labelled: bool = True,
) -> dict[str, Any]:
    """One node, with its colour, its diameter and its name already decided.

    An unlabelled node keeps everything else it had. Only the text goes, and
    :func:`card_of` reads the subgraph rather than this element, so the details
    column still names whatever is tapped.
    """
    return {
        "group": "nodes",
        "data": {
            "id": node.id,
            "kind": node.kind.value,
            "label": (node.label or node.id) if labelled else "",
            "weight": _weight_of(node, size_by),
            "color": colours[node.kind.value],
        },
    }


def _edge_element(edge: GraphEdge) -> dict[str, Any]:
    """One edge, keyed by its two ends and its kind.

    Cytoscape refuses a duplicate id, and a subgraph legitimately holds two
    edges between one pair when they mean different things — an address that
    both sent a message and was copied on it.
    """
    return {
        "group": "edges",
        "data": {
            "id": f"{edge.source}|{edge.kind}|{edge.target}",
            "source": edge.source,
            "target": edge.target,
            "kind": edge.kind,
            "label": edge.label,
            "weight": edge.weight,
        },
    }


def _weight_of(node: GraphNode, size_by: SizeBy) -> float:
    """The number *node* is drawn at, between 0 and 1."""
    if size_by is SizeBy.UNIFORM:
        return UNIFORM_WEIGHT
    return node.weights.get(size_by.value, UNIFORM_WEIGHT)
