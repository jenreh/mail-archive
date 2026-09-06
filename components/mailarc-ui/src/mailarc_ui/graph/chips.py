"""The reading pane's third tab: something on the header, drawn.

A message names the topic it is about, the tags a person put on it, the group
it went to, the people on it and — being a message — itself. Four of those are
rooted views of this explorer, so pressing one is a question the graph can
already answer, and answering it beside the mail is worth more than sending
somebody to another page to ask it again.

**This is where the arrow between the two packages is kept straight.**
:mod:`mailarc_ui.message_detail` shows the chips and remembers which one was
pressed; it may not draw one, because drawing needs
:func:`~mailarc_ui.graph.model.elements_of` and this package already reads that
one for its own reading column. So the pane leaves a hook —
``MessageDetailState._load_chip_graph`` — and a state that lists
:class:`ChipGraphState` instead of the pane's own mixin fills it in. The
component half is the same trade: :func:`~mailarc_ui.message_detail.message_tabs`
takes the panel as an argument rather than building one.

A recurring group is shown as a chip and is not pressable. There is no view
rooted at one and no reader that answers for one — see
:data:`~mailarc_ui.message_detail.model.DRAWABLE_CHIPS`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

import appkit_mantine as mn
import reflex as rx

from mailarc_analytics import Subgraph
from mailarc_analytics.queries.graphs import GraphReader
from mailarc_ui.graph.model import (
    DARK_STYLESHEET,
    LIGHT_STYLESHEET,
    GraphView,
    LayoutName,
    defaults_for,
    elements_of,
    layout_of,
)
from mailarc_ui.graph.reads import answered, graph_reader
from mailarc_ui.kit import graph_canvas, spinner
from mailarc_ui.message_detail import COLUMN, ChipKind, MessageDetailState
from mailarc_ui.shell import routes

logger = logging.getLogger(__name__)

CHIP_LIMIT = 60
"""How many nodes one chip's picture is drawn at.

Well under the explorer's own cap: this canvas is a column of the reading pane
rather than a page, and a topic of four hundred messages drawn in it is a grey
cloud. What it is for is seeing the shape of the thing and finding the way into
the explorer, which the panel links to.
"""

NOTHING_TO_DRAW = "Nothing to draw — a rebuild may have replaced this one."
"""R7 in the pane: a topic's id is a digest of its members and every rebuild
mints it afresh, so a chip read a moment ago can name something that is no
longer there."""

_READERS = {
    ChipKind.TOPIC.value: GraphView.TOPIC,
    ChipKind.TAG.value: GraphView.TAG,
    ChipKind.MESSAGE.value: GraphView.MESSAGE,
    ChipKind.ADDRESS.value: GraphView.ADDRESS,
}
"""Which view each kind the header offers opens. Exactly
:data:`~mailarc_ui.message_detail.model.DRAWABLE_CHIPS`, which a test checks —
something the header offers and this cannot read would be a tab that never
fills.
"""

_PICTURES: Mapping[GraphView, Callable[[GraphReader, str], Subgraph]] = (
    MappingProxyType(
        {
            GraphView.TOPIC: lambda reader, root: reader.topic(root, limit=CHIP_LIMIT),
            GraphView.TAG: lambda reader, root: reader.tag(root, limit=CHIP_LIMIT),
            GraphView.ADDRESS: lambda reader, root: reader.address(
                root, limit=CHIP_LIMIT
            ),
            GraphView.MESSAGE: lambda reader, root: reader.message(
                root, depth=1, limit=CHIP_LIMIT
            ),
        }
    )
)
"""How each of those views is actually read.

A map and not a branch, so adding a view is one line here and one in
:data:`_READERS` rather than a case in the middle of a function — and a test
holds the two together. A message is read at depth one: what a glyph beside a
subject asks is "what is around this mail", and the reply chain two hops out
is the explorer's question.
"""


IDLE_LAYOUT = layout_of(LayoutName.COSE)
"""What the canvas is told to do while it has nothing to draw.

A real arrangement and not ``{}``, which is the shape a Reflex ``dict`` var
defaults to: an empty object is truthy in the browser and cytoscape throws
"A `name` must be specified to make a layout" on one, taking the page with it.
Reachable whenever a read fails — the tab is opened before the answer comes
back. ``graph_canvas.jsx`` guards the same thing from the other side; this is
the half that a test can hold.
"""


def _picture(kind: str, chip_id: str) -> Subgraph:
    """The subgraph one pressed thing stands for, read afresh."""
    return _PICTURES[_READERS[kind]](graph_reader(), chip_id)


class ChipGraphState(MessageDetailState, mixin=True):
    """The reading pane, plus the picture behind one of its chips.

    A mixin over a mixin: a page that lists this one gets everything
    :class:`~mailarc_ui.message_detail.MessageDetailState` brings and a working
    third tab on top. Its vars are all prefixed, because the explorer's own
    state lists it beside the ones that draw the *page's* canvas and two
    ``elements`` would be one picture overwriting the other.
    """

    chip_elements: list[dict[str, Any]] = []
    """What the chip's canvas draws. Plain dicts for the reason
    :attr:`~mailarc_ui.graph.GraphExplorerState.elements` gives."""

    chip_graph_layout: dict[str, Any] = IDLE_LAYOUT
    """How to arrange them, never empty — see :data:`IDLE_LAYOUT`.

    Not ``chip_layout``: a name that short reads like the chip's own styling
    next to ``chip_label``.
    """

    chip_error: str = ""
    """What the graph said when it did not answer."""

    chip_notice: str = ""
    """Why the canvas is empty when nothing failed."""

    loading_chip: bool = False

    async def _load_chip_graph(self) -> None:
        """Fill the third tab from the chip the pane recorded.

        The hook :class:`~mailarc_ui.message_detail.MessageDetailState` leaves
        open. A kind with no view behind it draws nothing rather than raising:
        the header does not offer such a chip as pressable, so arriving here is
        a bug in *this* pair of collaborators and one empty panel is the
        proportionate way to survive it.
        """
        self._clear_chip_picture()
        if self.chip_kind not in _READERS:
            logger.debug("No graph view is rooted at a %s", self.chip_kind)
            self.chip_notice = NOTHING_TO_DRAW
            return
        kind, chip_id = self.chip_kind, self.chip_id
        self.loading_chip = True
        try:
            found, failure = await answered(
                lambda: _picture(kind, chip_id),
                f"draw the {kind} {chip_id}",
                Subgraph(),
            )
        finally:
            self.loading_chip = False
        if kind != self.chip_kind or chip_id != self.chip_id:
            logger.debug("A later chip won the tab; dropping the %s answer", kind)
            return
        self.chip_error = failure
        if failure:
            return
        self._draw_chip(found)

    def _draw_chip(self, found: Subgraph) -> None:
        """One answer, projected onto what the canvas takes."""
        if not found.nodes:
            self.chip_notice = NOTHING_TO_DRAW
            return
        size_by, layout = defaults_for(_READERS[self.chip_kind])
        self.chip_elements = elements_of(found, size_by=size_by)
        self.chip_graph_layout = layout_of(layout)

    def _clear_chip_picture(self) -> None:
        self.chip_elements = []
        self.chip_graph_layout = IDLE_LAYOUT
        self.chip_error = self.chip_notice = ""


def chip_graph_panel(state: type[ChipGraphState]) -> rx.Component:
    """The tab's body: the chip's corner of the graph, and the way out of it.

    Takes the concrete state class the way every part of the reading pane
    does, so one function serves the search page and the explorer's own
    reading column while each keeps its own open chip.
    """
    return mn.stack(
        rx.cond(
            state.loading_chip,
            spinner(),
            graph_canvas(
                elements=state.chip_elements,
                # The one decision this module makes, and the same one
                # `graph/components.py` makes for the page's canvas: a colour
                # scheme is a fact about the browser, so neither the state nor
                # the projection may hold it.
                stylesheet=rx.color_mode_cond(
                    light=LIGHT_STYLESHEET, dark=DARK_STYLESHEET
                ),
                layout=state.chip_graph_layout,
            ),
        ),
        rx.cond(state.chip_error != "", _chip_message(state.chip_error), rx.fragment()),
        rx.cond(
            state.chip_notice != "", _chip_message(state.chip_notice), rx.fragment()
        ),
        rx.link(
            mn.group(
                rx.icon("external-link", size=14),
                mn.text("Open in the explorer", size="xs"),
                gap=6,
                align="center",
            ),
            href=f"{routes.GRAPH}?view={state.chip_kind}&id={state.chip_id}",
            class_name="ma-chip-graph-link",
        ),
        gap="xs",
        # The reading pane's own sizing, imported rather than restated: a
        # percentage height has nothing definite to resolve against through a
        # tab panel, and this panel is in the same strip as the other two.
        style=COLUMN,
    )


def _chip_message(text: rx.Var | str) -> rx.Component:
    """One quiet line under the canvas — an outage, or why it is empty."""
    return mn.text(text, size="xs", c="dimmed")
