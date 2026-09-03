"""The explorer: which corner of the graph is drawn, and what is picked in it.

Three states in one class, and they are separable on purpose. The **question** —
a view, a root, a depth — is what a URL carries and what a rebuild can
invalidate. The **picture** is a
:class:`~mailarc_analytics.Subgraph`, held once and redrawn without asking the
graph anything: sizing by importance, hiding the addresses and changing the
layout are all rearrangements of an answer the page already has, and a page that
re-read for one of them would stutter on every dropdown. The **selection** is
what a person clicked, which is a message to read or a cluster to promote.

Two collaborators are mixed in rather than written again.
:class:`~mailarc_ui.message_detail.state.MessageDetailState` is the reading pane
— picking a message node here opens exactly what picking a search hit opens —
and :class:`~mailarc_ui.tags.state.TagActionsState` is the annotation layer,
whose one hook (:meth:`GraphExplorerState._cluster_members`) this class fills in
from the graph.

**The state's own view is called** :attr:`~GraphExplorerState.view_name`, and
that is not a preference: ``MessageDetailState.view`` is the open message, and a
second var of that name would silently replace it — the reading pane would then
be asked for ``view.body_html`` on a string. The URL parameter stays ``?view=``,
which is what a person reads.

R7 is the other thing this module is careful about. A ``Topic.id`` is a digest
of its members and is minted afresh by every rebuild, so ``/graph?view=topic&id=…``
goes stale by design; a link that no longer resolves gets a sentence saying so
rather than an empty canvas, because an empty canvas reads as "this topic is
empty" and that is a different, false claim.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import reflex as rx

from mailarc_analytics import NodeKind, Subgraph
from mailarc_ui.graph.model import (
    GraphView,
    LayoutName,
    NodeCard,
    SizeBy,
    card_of,
    defaults_for,
    elements_of,
    layout_of,
    stylesheet_of,
)
from mailarc_ui.graph.reads import (
    answered,
    archive_reader,
    cluster_members,
    graph_reader,
    picker_options,
    view_of,
)
from mailarc_ui.message_detail import MessageDetailState
from mailarc_ui.tags.state import TagActionsState, read_tags
from mailarc_ui.theme import Palette

logger = logging.getLogger(__name__)

MAX_DEPTH = 3
"""How far a message view will follow a reply chain.

Three, because that is what the statement behind it walks: a variable-length
quantifier is Cypher syntax rather than a bound value, so a deeper walk is a
promise this page cannot keep by binding anything.
"""

RECOMPUTED = (
    "This topic was recomputed — pick it again. A cluster's id is a digest of "
    "its members, so every rebuild mints a new one; a tag is the reference "
    "that survives."
)
"""R7, in the words somebody following a stale link needs.

Not an empty canvas: an empty canvas says "this topic holds nothing", which is
a claim, and it would be a false one.
"""

RECOMPUTED_CIRCLE = RECOMPUTED.replace("This topic", "This circle")
"""The same fact about a community, whose id is a digest of its members too."""

NOTHING_HERE = "The archive has nothing to draw for that."
"""What a root that resolves to no nodes says when it is not a cluster — an
address nobody wrote to, a tag with no messages left on it."""

NOTHING_DERIVED = (
    "Nothing to draw yet. The map is built out of what a rebuild derived — "
    "run one from Insights, then come back."
)
"""And what the map says over an archive nobody has rebuilt."""

PICK_ONE = "Pick something to look at."
"""The overview is rooted at nothing; every other view needs a root, and until
it has one there is no question to answer."""


def _sheet(palette: Mapping[str, str]) -> list[dict[str, Any]]:
    """The canvas' rules for one colour scheme, kinds coloured by selector.

    :func:`~mailarc_ui.graph.model.stylesheet_of` fills a node from
    ``data(color)``, which is what lets one stylesheet serve a canvas whose
    elements already know their colour. Here the *elements* are the thing that
    must not change with the colour scheme — they are the biggest var on the
    page and the picture is the same picture in both schemes — so a per-kind
    rule is appended for each, and a later rule wins in cytoscape.
    """
    return [
        *stylesheet_of(palette),
        *(
            {
                "selector": f'node[kind = "{kind.value}"]',
                "style": {"background-color": palette[kind.value]},
            }
            for kind in NodeKind
        ),
    ]


LIGHT_STYLESHEET = _sheet(Palette.LIGHT)
DARK_STYLESHEET = _sheet(Palette.DARK)
"""The two stylesheets, built once at import.

Not state, and deliberately: which one a reader sees is a fact about their
browser rather than about the archive, so
:mod:`mailarc_ui.graph.components` picks between them with
``rx.color_mode_cond`` and the state never learns which. A var that only ever
held one value would be a state that could disagree with the screen.
"""


class GraphExplorerState(TagActionsState, MessageDetailState, rx.State):
    """One corner of the graph: what to draw, what was drawn, what is picked."""

    view_name: str = GraphView.OVERVIEW.value
    """Which kind of thing the picture is rooted at. See the module docstring
    for why it is not called ``view``."""

    picked_id: str = ""
    """What it is rooted *at* — a topic digest, an address, a tag id."""

    options: list[dict[str, str]] = []
    """What the root picker offers, in the record shape ``mn.select`` takes."""

    depth: int = 1
    """How far a message view follows its reply chain. Nothing else uses it."""

    size_by: str = SizeBy.UNIFORM.value
    layout_name: str = LayoutName.COSE.value
    hidden_kinds: list[str] = []

    elements: list[dict[str, Any]] = []
    """What the canvas draws, from
    :func:`~mailarc_ui.graph.model.elements_of`.

    Plain dicts and not value objects, the documented exception the dashboard's
    chart series makes: ``elements`` on the wrapper is
    ``Var[list[dict[str, Any]]]``, and a model handed to it is a prop Reflex
    refuses at build time. The shape is built in one place, so no component
    assembles a record.
    """

    layout: dict[str, Any] = {}
    """How to arrange them — the same argument as :attr:`elements`."""

    selected_node: NodeCard = NodeCard()
    """The node the details column is about. Empty is the "nothing is picked"
    sentinel, so no component in that column has to guard on ``None``."""

    cluster_rows: list[NodeCard] = []
    """The messages of the picked topic or circle that are *on the canvas*.

    What is drawn and not what the cluster holds, because those differ: the
    picture is capped at a canvas' worth of nodes. Promoting takes the whole
    cluster (:meth:`_cluster_members` reads it afresh), and the column says so
    — two numbers for one thing is worse than one honest one.
    """

    fit_token: int = 0
    """Bumped to re-fit the viewport. A counter and not a flag: fitting a
    picture that is already fit has to fit it again."""

    loading: bool = False
    error: str = ""
    """What the graph said when it did not answer. Never a notice."""

    notice: str = ""
    """Why there is nothing on the canvas, when there is nothing and it is not
    a failure — a stale cluster id (R7), an archive with nothing derived, a view
    with no root picked yet."""

    truncated_notice: str = ""
    """Which read was cut, when the picture is only part of the answer."""

    _subgraph: Subgraph = Subgraph()
    """What was last read, before it was drawn.

    A backend var: nothing in the browser reads it — the elements are what the
    canvas takes and the card is what the column prints — and sending a second
    copy of the biggest thing on the page with every delta would pay twice for
    one answer. What it buys is that re-sizing, re-laying-out and hiding a kind
    are redraws rather than re-reads.
    """

    @rx.var
    def needs_a_root(self) -> bool:
        """Whether this view is one that has to be pointed at something."""
        return self.view_name != GraphView.OVERVIEW.value

    @rx.var
    def has_picture(self) -> bool:
        return len(self.elements) > 0

    @rx.var
    def is_message(self) -> bool:
        """Whether the details column should be the reading pane."""
        return self.selected_node.kind == NodeKind.MESSAGE.value

    @rx.var
    def is_cluster(self) -> bool:
        """Whether what is picked is something that can become a tag."""
        return self.selected_node.kind in (
            NodeKind.TOPIC.value,
            NodeKind.COMMUNITY.value,
        )

    @rx.event(background=True)
    async def load(self) -> None:
        """Draw whatever the address bar asks for. The page's ``on_load``.

        A background task, so the state lock is held around the mutations and
        never around the reads: a subgraph of a busy topic is real work, and a
        plain handler would freeze every other event on the session behind it.
        """
        async with self:
            self.view_name, self.picked_id = self._asked_for()
            self._adopt_defaults(self.view_name)
            self.loading = True
            self.error = ""
            wanted, root, depth = self.view_name, self.picked_id, self.depth
        found, failure = await self._read_view(wanted, root, depth)
        options, _ = await answered(
            lambda: picker_options(wanted), "list what a view roots at", []
        )
        tags, tag_failure = await read_tags()
        async with self:
            self.options = options
            self.tags, self.tag_error = tags, tag_failure
            self._apply(found, failure)

    @rx.event
    async def choose_view(self, value: str) -> None:
        """Look at a different kind of thing.

        The root goes with it and so does the picture: a topic id means nothing
        to the address view, and a canvas left holding the previous answer
        under a new heading is the worst of both.
        """
        kind = view_of(value)
        self.view_name = kind.value
        self.picked_id = ""
        self._adopt_defaults(kind)
        self.options, _ = await answered(
            lambda: picker_options(kind), "list what a view roots at", []
        )
        await self._reread()

    @rx.event
    async def pick(self, value: str) -> None:
        """Root the current view at *value* and draw it."""
        self.picked_id = value
        await self._reread()

    @rx.event
    async def set_depth(self, value: float | str) -> None:
        """How far to follow a reply chain.

        ``mn.number_input`` hands over a ``float | str`` — an emptied box
        arrives as ``""`` — so both are read here and neither is an error.

        Re-reads only where the number changes the answer, which is the message
        view: for every other view the depth is a setting that will apply to the
        next message somebody opens, and re-running a topic read on a keystroke
        would be work nobody asked for.
        """
        self.depth = _whole(value)
        if self.view_name == GraphView.MESSAGE.value:
            await self._reread()

    @rx.event
    def set_size_by(self, value: str) -> None:
        """Size the nodes by a different number — a redraw, never a re-read."""
        self.size_by = _size_of(value).value
        self._draw()

    @rx.event
    def set_layout(self, value: str) -> None:
        """Arrange the same nodes differently."""
        self.layout_name = _layout_of(value).value
        self._draw()

    @rx.event
    def toggle_kind(self, kind: str) -> None:
        """Show or hide one kind of node, and every edge that touched one.

        The edges are not a nicety: cytoscape throws on an edge whose end is
        not in the collection and the throw aborts the whole ``add``, so a
        single filtered-out node would leave the canvas blank with nothing in
        the console to explain it.
        """
        hidden = [one for one in self.hidden_kinds if one != kind]
        if len(hidden) == len(self.hidden_kinds):
            hidden.append(kind)
        self.hidden_kinds = hidden
        self._draw()

    @rx.event
    def fit(self) -> None:
        """Put the whole picture back in the viewport."""
        self.fit_token += 1

    @rx.event
    def clear_selection(self) -> None:
        """Nothing is picked any more — the canvas' own background was tapped."""
        self.selected_node = NodeCard()
        self.cluster_rows = []

    @rx.event
    async def select_node(self, node_id: str) -> None:
        """Show what one node is, and open it if it is a message.

        A node the picture does not hold is ignored rather than answered with
        an empty card: the only way to ask for one is a canvas and a state that
        have gone out of step, and blanking the column would hide that rather
        than survive it.
        """
        node = self._node(node_id)
        if node is None:
            logger.debug("Nothing in the picture is called %s", node_id)
            return
        self.selected_node = card_of(node)
        self.cluster_rows = self._drawn_members(node_id, node.kind)
        if node.kind is NodeKind.TAG:
            self._forget_message()
            await self.show_suggestions(node_id)
            return
        if node.kind is not NodeKind.MESSAGE:
            self._forget_message()
            return
        await self._open_from_graph(node_id)
        await self.read_message_tags(node_id)

    @rx.event
    async def expand_node(self, node_id: str) -> None:
        """One hop out of a node, laid over the picture rather than replacing it.

        What a double-click means: the answer to "and what else is around this"
        goes *beside* the answer already on screen, which is what
        :meth:`~mailarc_analytics.Subgraph.merged_with` guarantees is still
        drawable.
        """
        node = self._node(node_id)
        if node is None:
            logger.debug("Nothing in the picture is called %s", node_id)
            return
        kind = node.kind
        found, failure = await answered(
            lambda: graph_reader().expand(node_id, kind),
            f"expand {node_id}",
            Subgraph(),
        )
        self.error = failure
        if failure:
            return
        self._merge(found)

    @rx.event
    async def show_path(self, other_id: str) -> None:
        """How the root of this view and the picked node are connected.

        Over ``CO_ADDRESSED`` and in both directions, which is what makes the
        answer readable — a route through the messages themselves would report
        twice the hop count a person would call it.
        """
        left = self.picked_id or self.selected_node.id
        if not left or not other_id or left == other_id:
            return
        found, failure = await answered(
            lambda: graph_reader().path(left, other_id),
            f"find a route from {left}",
            Subgraph(),
        )
        self.error = failure
        if failure:
            return
        if not found.nodes:
            self.tag_notice = "Nothing connects those two within a few hops."
            return
        self._merge(found)

    async def _cluster_members(self, kind: str, cluster_id: str) -> tuple[str, ...]:
        """What is in the cluster being promoted — the mixin's one hook.

        Read from the graph rather than taken off the picture: what is drawn is
        capped at a canvas' worth of nodes, and half a project is a worse tag
        than none.
        """
        members, failure = await answered(
            lambda: cluster_members(kind, cluster_id),
            f"read the members of {cluster_id}",
            (),
        )
        if failure:
            self.error = failure
        return members

    async def _read_view(
        self, view: str, root: str, depth: int
    ) -> tuple[Subgraph, str]:
        """One view of the graph, or a sentence saying why there is none."""
        kind = view_of(view)
        if kind is not GraphView.OVERVIEW and not root:
            return Subgraph(), ""
        return await answered(
            lambda: _asked(kind, root, depth),
            f"read the {kind.value} view",
            Subgraph(),
        )

    async def _reread(self) -> None:
        """Ask the graph the current question again and redraw the answer."""
        self.loading = True
        self.error = ""
        found, failure = await self._read_view(
            self.view_name, self.picked_id, self.depth
        )
        self._apply(found, failure)

    def _apply(self, found: Subgraph, failure: str) -> None:
        """One answer, become a picture. Called under the state lock."""
        self._subgraph = found
        self.error = failure
        self.truncated_notice = "" if failure else found.notice
        self.notice = "" if failure else self._why_empty(found)
        self.selected_node = NodeCard()
        self.cluster_rows = []
        self._forget_message()
        self._draw()
        self.loading = False

    def _merge(self, found: Subgraph) -> None:
        """Lay one more answer over the picture and redraw it."""
        merged = Subgraph(
            nodes=tuple(self._subgraph.nodes),
            edges=tuple(self._subgraph.edges),
            truncated=self._subgraph.truncated,
            notice=self._subgraph.notice,
        ).merged_with(found)
        self._subgraph = merged
        self.truncated_notice = merged.notice
        self.notice = ""
        self._draw()

    def _draw(self) -> None:
        """The picture, at the current size, arrangement and set of kinds."""
        self.elements = elements_of(
            self._subgraph,
            size_by=_size_of(self.size_by),
            hidden_kinds=list(self.hidden_kinds),
        )
        self.layout = layout_of(_layout_of(self.layout_name))
        self.fit_token += 1

    def _why_empty(self, found: Subgraph) -> str:
        """The sentence a blank canvas needs, or nothing because it is not blank."""
        if found.nodes:
            return ""
        kind = view_of(self.view_name)
        if kind is GraphView.OVERVIEW:
            return NOTHING_DERIVED
        if not self.picked_id:
            return PICK_ONE
        if kind is GraphView.TOPIC:
            return RECOMPUTED
        if kind is GraphView.COMMUNITY:
            return RECOMPUTED_CIRCLE
        return NOTHING_HERE

    def _adopt_defaults(self, view: GraphView | str) -> None:
        """Draw *view* the way that view wants to be drawn.

        Called where the root is already being reset — a page load and a change
        of view — because these two controls answer to the same thing the root
        does: what is on the canvas. A map wants its collections sized by how
        much they hold and ranked in rings; a rooted view wants neither.

        Not called from :meth:`pick`, :meth:`set_size_by` or
        :meth:`set_layout`: once somebody has touched a control, the picture is
        theirs and re-deciding it under them is the bug this replaces.
        """
        size_by, layout = defaults_for(view)
        self.size_by, self.layout_name = size_by.value, layout.value

    def _asked_for(self) -> tuple[str, str]:
        """The view and root the address bar names, or the ones already held.

        ``RouterData.page`` is deprecated; the parameters come off
        ``router.url``. A ``?view=`` nobody serves falls back to the overview
        *and drops the id with it* — an id minted for a view that no longer
        exists is not an id this one can use.
        """
        params = self.router.url.query_parameters
        wanted = params.get("view", "")
        if not wanted:
            return self.view_name, self.picked_id
        kind = view_of(wanted)
        if kind.value != wanted:
            return kind.value, ""
        return kind.value, params.get("id", self.picked_id)

    def _drawn_members(self, node_id: str, kind: NodeKind) -> list[NodeCard]:
        """The message nodes joined to this cluster in the picture on screen.

        Off the edges already held rather than out of a read: the answer is
        "what of this topic am I looking at", and that question has no answer
        anywhere but the drawing.
        """
        if kind not in (NodeKind.TOPIC, NodeKind.COMMUNITY):
            return []
        joined = {
            one.source if one.target == node_id else one.target
            for one in self._subgraph.edges
            if node_id in (one.source, one.target)
        }
        return [
            card_of(one)
            for one in self._subgraph.nodes
            if one.id in joined and one.kind is NodeKind.MESSAGE
        ]

    def _node(self, node_id: str) -> Any:
        """The node the picture holds under this id, or ``None``."""
        return next((one for one in self._subgraph.nodes if one.id == node_id), None)

    async def _open_from_graph(self, message_id: str) -> None:
        """Open a message the canvas named, in the pane beside it.

        The graph knows the id and the reading pane needs the digest of the
        stored original, so one hydration read sits between them — the same one
        every ranked answer in this application ends with.
        """
        summaries, failure = await answered(
            lambda: archive_reader().messages_by_ids([message_id]),
            f"look up {message_id}",
            [],
        )
        if failure:
            self.error = failure
            return
        digest = summaries[0].eml_sha256 if summaries else None
        await self._open_message(message_id, digest or "")

    def _forget_message(self) -> None:
        """Close whatever the reading pane was showing."""
        self.selected_id = ""
        self.message_tags = []
        self._clear_views()


def _asked(kind: GraphView, root: str, depth: int) -> Subgraph:
    """One read of the graph, chosen by view. Blocking — the caller threads it."""
    reader = graph_reader()
    if kind is GraphView.OVERVIEW:
        return reader.overview()
    if kind is GraphView.MESSAGE:
        return reader.message(root, depth=depth)
    if kind is GraphView.TOPIC:
        return reader.topic(root)
    if kind is GraphView.ADDRESS:
        return reader.address(root)
    if kind is GraphView.TAG:
        return reader.tag(root)
    return reader.community(root)


def _whole(value: float | str) -> int:
    """A depth out of whatever the number box sent, between one and the cap."""
    try:
        asked = int(float(value))
    except TypeError, ValueError:
        asked = 1
    return min(max(asked, 1), MAX_DEPTH)


def _size_of(value: str) -> SizeBy:
    """A weight to size by, or no claim about size at all."""
    try:
        return SizeBy(value)
    except ValueError:
        logger.debug("Unknown size %r — drawing every node the same", value)
        return SizeBy.UNIFORM


def _layout_of(value: str) -> LayoutName:
    """An arrangement, or the force layout that suits an unknown graph."""
    try:
        return LayoutName(value)
    except ValueError:
        logger.debug("Unknown layout %r — using %s", value, LayoutName.COSE.value)
        return LayoutName.COSE
