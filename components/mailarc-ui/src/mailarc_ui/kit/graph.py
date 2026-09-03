"""The graph canvas — the one React component this archive wraps itself.

Everything else in the kit is a recipe over an ``mn.*`` component. This one is
a foreign library with an imperative API and a DOM node of its own, and it is
here for the same reason the rest of the kit is: the design has exactly one
canvas, and a page that reached for the wrapper directly would be a page
deciding for itself how tall it is and what a selected node looks like.

The split between the two files beside each other is deliberate. Everything
that can be decided in Python is —
:func:`mailarc_ui.graph.model.elements_of` colours and sizes every node,
:func:`~mailarc_ui.graph.model.stylesheet_of` states the rules,
:func:`~mailarc_ui.graph.model.layout_of` names the arrangement — and
``graph_canvas.jsx`` is left with the part that genuinely needs a browser: an
instance that owns a ``<div>``, survives re-renders and reports taps back. That
is what keeps the interesting decisions testable without a browser.

:class:`_GraphCanvas` stays private. The wrapper's props are a contract with
one JSX file, not a vocabulary pages should learn; what the kit exports is
:func:`graph_canvas`, which is the wrapper inside the box that gives it a
height. Cytoscape measures its container on mount, and a container that is
still nought pixels tall draws an empty canvas without reporting anything.
"""

from __future__ import annotations

from typing import Any

import reflex as rx
from reflex.components.component import NoSSRComponent
from reflex.event import EventHandler
from reflex.utils.imports import ImportDict
from reflex.vars.base import Var

_GRAPH_JSX = rx.asset("graph_canvas.jsx", shared=True)
"""The local wrapper, symlinked into the compiling app's ``assets/external``."""

_GRAPH_LIBRARY = _GRAPH_JSX.importable_path
"""``importable_path`` omits the ``?v=`` content hash, which Vite would treat
as an optimised-dep URL and cache immutably — pinning a stale React instance.
The same reason ``appkit_mantine.tiptap`` gives."""

CYTOSCAPE = "cytoscape@3.34.2"
"""Pinned rather than ranged: a graph library's defaults *are* the design, and
a minor release that re-tunes a layout would silently redraw every picture."""

REACT_CYTOSCAPE = "react-cytoscapejs@2.0.0"
"""Declared but not imported by the wrapper, which drives cytoscape directly.

It is here because it is the React binding this canvas would move to if the
imperative wrapper ever stopped paying for itself, and having it resolved keeps
that a change to one JSX file rather than to the dependency set. Nothing
imports it, so nothing of it is bundled."""


def _no_arguments() -> list[None]:
    """The event spec for a callback the JSX calls with nothing.

    A named function rather than ``lambda: []`` because ``ruff``'s PIE807
    rewrites that to ``EventHandler[list]``, and rather than ``EventHandler[list]``
    because Reflex reads the spec's *signature* to name the event's arguments:
    ``list`` has one — an unannotated ``iterable`` — so the rewrite raises
    ``MissingAnnotationError`` the moment a page hands the trigger a handler.
    """
    return []


CANVAS_HEIGHT = "var(--ma-graph-height)"
"""The box's height, stated as the token the stylesheet defines it under.

Inline as well as in ``.ma-graph-canvas`` because this is the one measurement
the component *needs* resolved at mount rather than at paint: cytoscape reads
its container's height once when it is created."""


class _GraphCanvas(NoSSRComponent):
    """The cytoscape instance, loaded only in the browser.

    ``NoSSRComponent`` because cytoscape touches ``document`` at construction —
    rendered on the server it throws — and because a graph library is four
    hundred kilobytes that only ``/graph`` should pay for. It arrives as its
    own lazy chunk.

    Props are camelCased on their way out, so :attr:`fit_token` reaches the JSX
    as ``fitToken``.
    """

    library = _GRAPH_LIBRARY
    tag = "GraphCanvas"
    is_default = True

    lib_dependencies: list[str] = [CYTOSCAPE, REACT_CYTOSCAPE]

    elements: Var[list[dict[str, Any]]]
    """What to draw, from :func:`~mailarc_ui.graph.model.elements_of`. Nodes
    first: cytoscape refuses an edge whose ends are not in yet."""

    stylesheet: Var[list[dict[str, Any]]]
    """How to draw it, from :func:`~mailarc_ui.graph.model.stylesheet_of` —
    with the palette's hexes already substituted in, because a canvas cannot
    resolve a CSS custom property."""

    layout: Var[dict[str, Any]]
    """How to arrange it, from :func:`~mailarc_ui.graph.model.layout_of`."""

    selected: Var[str]
    """The id of the one selected node, or empty. The canvas follows this
    rather than owning the selection, so a node picked from a table beside it
    and one tapped on the canvas are the same state."""

    fit_token: Var[int]
    """Bumped to re-fit the viewport.

    A counter and not a flag: pressing "Fit" on a picture that is already fit
    has to fit it again, and there is no other state change to observe.
    """

    on_select: EventHandler[  # ty: ignore[invalid-type-form]
        lambda node_id: [node_id]
    ]
    """A node was tapped — its id."""

    on_expand: EventHandler[  # ty: ignore[invalid-type-form]
        lambda node_id: [node_id]
    ]
    """A node was double-tapped: fetch one hop and lay it over the picture."""

    on_background: EventHandler[_no_arguments]  # ty: ignore[invalid-type-form]
    """The canvas itself was tapped — nothing is selected any more.

    See :func:`_no_arguments` for why the spec is a named function.
    """

    def add_imports(self) -> ImportDict:
        """The lazy-loading machinery the dynamic import is written against."""
        return {
            "react": ["lazy"],
            f"$/{rx.constants.Dirs.UTILS}/context": ["ClientSide"],
        }


def graph_canvas(**props: Any) -> rx.Component:
    """The canvas, in the sized box that makes it visible.

    Every prop goes through to the wrapper; the box only supplies the class the
    stylesheet keys on and the height cytoscape measures.
    """
    return rx.box(
        _GraphCanvas.create(**props),
        class_name="ma-graph-canvas",
        height=CANVAS_HEIGHT,
    )
