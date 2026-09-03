"""What the explorer hands cytoscape, checked without a browser in the room.

Three functions and no I/O: a :class:`~mailarc_analytics.Subgraph` goes in and
a list of element dicts, a stylesheet and a layout come out. That is the whole
of what can be wrong here *and* the whole of what a running page cannot tell
you — a canvas that throws on a dangling edge draws nothing at all and says so
only in the browser console, and a node sized off a weight it does not carry is
a picture that reads as a fact.

The last class is the one worth keeping honest. A kind's colour lives twice on
purpose — once as ``--ma-graph-<kind>``, which is what the legend dot beside
the canvas wears, and once in :class:`~mailarc_ui.theme.Palette`, which is what
the canvas itself is handed, because cytoscape paints into a ``<canvas>`` and
cannot read a custom property. Two copies of a palette are two copies that
drift, so the stylesheet is parsed and compared. The halo colour is checked
against ``--ma-surface`` for the same reason: a node's hairline ring only reads
as a gap while it matches the ground the canvas is painted on.
"""

import re
from pathlib import Path

from mailarc_analytics import GraphEdge, GraphNode, NodeKind, Subgraph, Weight
from mailarc_ui.graph.model import (
    LABEL_DENSITY_LIMIT,
    LABELLED_MAX,
    NODE_MAX_SIZE,
    NODE_MIN_SIZE,
    UNIFORM_WEIGHT,
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
from mailarc_ui.theme import Palette

STYLESHEET = Path(__file__).parents[3] / "assets" / "css" / "mail-archive.css"
"""The application's own stylesheet, found from the test rather than from the
installed package — an editable install resolves to the same tree, and this
file is in the repository whatever the install looks like."""


def _subgraph() -> Subgraph:
    """A message, its sender and the topic it is filed under."""
    return Subgraph(
        nodes=(
            GraphNode(
                id="<m1@example.test>",
                kind=NodeKind.MESSAGE,
                label="Re: NORD-42",
                weights={Weight.IMPORTANCE.value: 0.8, Weight.DEGREE.value: 1.0},
                props={"sent_at": "2026-04-01"},
            ),
            GraphNode(
                id="anna@kunde.example",
                kind=NodeKind.ADDRESS,
                label="anna@kunde.example",
                weights={Weight.PAGERANK.value: 0.25},
            ),
            GraphNode(id="topic:abc", kind=NodeKind.TOPIC, label=""),
        ),
        edges=(
            GraphEdge(
                source="anna@kunde.example",
                target="<m1@example.test>",
                kind="SENT",
            ),
            GraphEdge(
                source="<m1@example.test>",
                target="topic:abc",
                kind="ABOUT",
                weight=0.9,
                label="subject",
            ),
        ),
    )


def _nodes(elements: list[dict]) -> dict[str, dict]:
    """The node elements, by id."""
    return {
        one["data"]["id"]: one["data"] for one in elements if one["group"] == "nodes"
    }


def _edges(elements: list[dict]) -> list[dict]:
    """The edge elements, in the order they were drawn."""
    return [one["data"] for one in elements if one["group"] == "edges"]


def _rule(sheet: list[dict], selector: str) -> dict:
    """The one rule for *selector*."""
    return next(one["style"] for one in sheet if one["selector"] == selector)


def _declared(block: str) -> dict[str, str]:
    """Every ``--ma-graph-<name>: #hex`` in one CSS block, by name."""
    return {
        name: value.lower()
        for name, value in re.findall(
            r"--ma-graph-([a-z-]+):\s*(#[0-9a-fA-F]{6})", block
        )
    }


def _kinds(scheme: dict[str, str]) -> dict[str, str]:
    """The six kind colours of one scheme, lower-cased as the stylesheet has
    them. The three canvas-only ones have no token to compare against."""
    return {one.value: scheme[one.value].lower() for one in NodeKind}


def _surface(block: str) -> str:
    """The ``--ma-surface`` one CSS block declares."""
    found = re.search(r"--ma-surface:\s*(#[0-9a-fA-F]{6})", block)

    assert found is not None, "every scheme block states a surface"
    return found.group(1).lower()


def _block(css: str, opener: str) -> str:
    """The declarations of the first block *opener* opens."""
    start = css.index(opener) + len(opener)
    return css[start : css.index("\n}", start)]


class TestElements:
    """One subgraph, one list cytoscape will accept."""

    def test_every_node_becomes_an_element_carrying_its_kind_and_colour(self) -> None:
        drawn = _nodes(elements_of(_subgraph(), size_by=SizeBy.UNIFORM))

        assert set(drawn) == {"<m1@example.test>", "anna@kunde.example", "topic:abc"}
        assert drawn["anna@kunde.example"]["kind"] == NodeKind.ADDRESS.value
        assert drawn["anna@kunde.example"]["color"] == Palette.LIGHT["address"]

    def test_a_node_with_no_label_is_drawn_by_its_id(self) -> None:
        """An empty label is what the store had, not what the reader draws —
        an unlabelled circle is a node nobody can say anything about."""
        drawn = _nodes(elements_of(_subgraph(), size_by=SizeBy.UNIFORM))

        assert drawn["topic:abc"]["label"] == "topic:abc"
        assert drawn["<m1@example.test>"]["label"] == "Re: NORD-42"

    def test_an_edge_is_keyed_by_its_two_ends_and_its_kind(self) -> None:
        """Cytoscape refuses a duplicate id, and a subgraph may hold two edges
        between the same pair as long as they mean different things."""
        drawn = _edges(elements_of(_subgraph(), size_by=SizeBy.UNIFORM))

        assert [one["id"] for one in drawn] == [
            "anna@kunde.example|SENT|<m1@example.test>",
            "<m1@example.test>|ABOUT|topic:abc",
        ]
        assert drawn[1]["label"] == "subject"
        assert drawn[1]["weight"] == 0.9

    def test_size_by_picks_the_number_the_page_chose(self) -> None:
        drawn = _nodes(elements_of(_subgraph(), size_by=SizeBy.IMPORTANCE))

        assert drawn["<m1@example.test>"]["weight"] == 0.8

    def test_a_node_missing_that_number_is_not_the_smallest_circle(self) -> None:
        """A sparse weight means *this node has no such number*, which is not
        zero — drawn at zero it reads as the least important thing on screen."""
        drawn = _nodes(elements_of(_subgraph(), size_by=SizeBy.IMPORTANCE))

        assert drawn["anna@kunde.example"]["weight"] == UNIFORM_WEIGHT

    def test_uniform_draws_every_node_the_same_size(self) -> None:
        drawn = _nodes(elements_of(_subgraph(), size_by=SizeBy.UNIFORM))

        assert {one["weight"] for one in drawn.values()} == {UNIFORM_WEIGHT}

    def test_a_hidden_kind_takes_its_nodes_off_the_canvas(self) -> None:
        drawn = _nodes(
            elements_of(
                _subgraph(), size_by=SizeBy.UNIFORM, hidden_kinds=[NodeKind.TOPIC.value]
            )
        )

        assert "topic:abc" not in drawn

    def test_an_edge_whose_end_was_hidden_goes_with_it(self) -> None:
        """The one that is not cosmetic: cytoscape throws on an edge whose
        endpoint is missing, and a thrown ``add`` leaves the canvas empty."""
        drawn = _edges(
            elements_of(
                _subgraph(), size_by=SizeBy.UNIFORM, hidden_kinds=[NodeKind.TOPIC.value]
            )
        )

        assert [one["kind"] for one in drawn] == ["SENT"]

    def test_the_dark_palette_colours_the_same_graph_differently(self) -> None:
        light = _nodes(elements_of(_subgraph(), size_by=SizeBy.UNIFORM))
        dark = _nodes(
            elements_of(
                _subgraph(), size_by=SizeBy.UNIFORM, palette=Palette.of(dark=True)
            )
        )

        assert dark["topic:abc"]["color"] == Palette.DARK["topic"]
        assert dark["topic:abc"]["color"] != light["topic:abc"]["color"]

    def test_an_empty_subgraph_draws_nothing_rather_than_failing(self) -> None:
        assert elements_of(Subgraph(), size_by=SizeBy.DEGREE) == []


class TestSizeBy:
    def test_every_number_the_reader_normalises_can_be_picked(self) -> None:
        """``SizeBy`` is ``Weight`` plus ``uniform``; a value that did not match
        would be a dropdown entry that sizes nothing and raises nothing."""
        assert {one.value for one in Weight} < {one.value for one in SizeBy}
        assert SizeBy.UNIFORM.value == "uniform"


class TestStylesheet:
    """The rules, with the palette's hexes already substituted in."""

    def test_a_node_is_sized_off_its_own_weight(self) -> None:
        node = _rule(stylesheet_of(Palette.LIGHT), "node")
        mapped = f"mapData(weight, 0, 1, {NODE_MIN_SIZE}, {NODE_MAX_SIZE})"

        assert node["width"] == mapped
        assert node["height"] == mapped

    def test_a_node_is_filled_from_its_own_data(self) -> None:
        """One rule for six kinds — the colour rides with the element, which is
        what lets ``elements_of`` decide it per colour scheme."""
        assert _rule(stylesheet_of(Palette.LIGHT), "node")["background-color"] == (
            "data(color)"
        )

    def test_the_palette_reaches_the_labels_and_the_lines(self) -> None:
        sheet = stylesheet_of(Palette.DARK)

        assert _rule(sheet, "node")["color"] == Palette.DARK["text"]
        assert _rule(sheet, "edge")["line-color"] == Palette.DARK["edge"]

    def test_a_selected_node_wears_the_selection_colour(self) -> None:
        border = _rule(stylesheet_of(Palette.LIGHT), "node:selected")

        assert border["border-color"] == Palette.LIGHT["selected"]

    def test_a_label_is_read_off_the_element(self) -> None:
        assert _rule(stylesheet_of(Palette.LIGHT), "node")["label"] == "data(label)"


class TestLayout:
    """Three named layouts, and one of them is the fallback."""

    def test_each_name_produces_the_layout_it_names(self) -> None:
        for name in LayoutName:
            assert layout_of(name)["name"] == name.value

    def test_no_layout_animates(self) -> None:
        """A canvas that settles while a reader is already clicking is a canvas
        whose nodes move out from under the pointer."""
        assert all(layout_of(name)["animate"] is False for name in LayoutName)

    def test_the_force_layout_does_not_randomise(self) -> None:
        """Two rebuilds of the same subgraph have to draw the same picture, or
        a reader cannot tell a changed archive from a re-run layout."""
        assert layout_of(LayoutName.COSE)["randomize"] is False

    def test_a_name_from_an_older_link_falls_back_rather_than_raising(self) -> None:
        assert layout_of("fcose")["name"] == LayoutName.COSE.value


class TestNodeCard:
    """What the details column prints once a node is picked."""

    def test_a_card_takes_its_title_from_the_label(self) -> None:
        card = card_of(_subgraph().nodes[0])

        assert card.title == "Re: NORD-42"
        assert card.kind == NodeKind.MESSAGE.value
        assert card.id == "<m1@example.test>"

    def test_a_node_with_no_label_is_titled_by_its_id(self) -> None:
        assert card_of(_subgraph().nodes[2]).title == "topic:abc"

    def test_the_props_become_lines_in_a_stable_order(self) -> None:
        card = card_of(
            GraphNode(
                id="x",
                kind=NodeKind.MESSAGE,
                props={"sent_at": "2026-04-01", "domain": "kunde.example"},
            )
        )

        assert card.lines == ("domain: kunde.example", "sent at: 2026-04-01")

    def test_nothing_selected_is_an_empty_card_rather_than_none(self) -> None:
        """The sentinel every component in the details column guards on — the
        same argument ``insights.model.NO_TOTALS`` makes."""
        assert NodeCard().title == ""
        assert NodeCard().lines == ()


class TestTheViews:
    def test_each_view_is_a_word_a_query_parameter_can_carry(self) -> None:
        assert GraphView.TOPIC.value == "topic"
        assert {one.value for one in GraphView} >= {one.value for one in NodeKind} - {
            NodeKind.THREAD.value
        }


class TestThePaletteHasTwoHomes:
    """A kind's colour is stated twice, and the two copies have to agree.

    Only the six kinds: the edge, the label ink and the selection ring are
    never drawn as DOM, so they are Python-only — a ``--ma-*`` token nothing
    paints is what ``tests/test_stylesheets.py`` fails a run over.
    """

    def test_every_kind_has_a_colour_in_both_schemes(self) -> None:
        for scheme in (Palette.LIGHT, Palette.DARK):
            assert {one.value for one in NodeKind} <= set(scheme)
            assert {"edge", "text", "selected", "surface"} <= set(scheme)

    def test_the_stylesheet_states_the_light_hues(self) -> None:
        declared = _declared(_block(STYLESHEET.read_text(encoding="utf-8"), ":root {"))

        assert declared == _kinds(Palette.LIGHT)

    def test_the_stylesheet_states_the_dark_hues(self) -> None:
        declared = _declared(
            _block(
                STYLESHEET.read_text(encoding="utf-8"),
                '[data-mantine-color-scheme="dark"] {',
            )
        )

        assert declared == _kinds(Palette.DARK)

    def test_the_halo_is_the_ground_the_canvas_is_painted_on(self) -> None:
        """``stylesheet_of`` rings every node in ``surface`` to keep two
        touching circles apart; ``.ma-graph-canvas`` fills itself with
        ``--ma-surface``. Drift and the ring becomes a visible rim."""
        css = STYLESHEET.read_text(encoding="utf-8")

        assert _surface(_block(css, ":root {")) == Palette.LIGHT["surface"].lower()
        assert (
            _surface(_block(css, '[data-mantine-color-scheme="dark"] {'))
            == Palette.DARK["surface"].lower()
        )


def _crowd(count: int) -> Subgraph:
    """*count* topics of descending weight and no edges at all.

    The overview's real shape on a live archive: the map draws collections and
    joins them only through circles and tags, so an archive that has neither
    hands the canvas a bag of unconnected dots. Measured on a real 846-message
    archive: 75 topics, 0 communities, 0 tags, 0 edges.
    """
    return Subgraph(
        nodes=tuple(
            GraphNode(
                id=f"topic:{index:03d}",
                kind=NodeKind.TOPIC,
                label=f"a topic called number {index}",
                weights={Weight.COUNT.value: (count - index) / count},
            )
            for index in range(count)
        ),
        edges=(),
    )


class TestTheMapIsLegibleWhenItIsCrowded:
    """The three defaults that decide whether the overview reads as anything.

    Reported from a real archive: seventy-five equal dots in a grid, labels
    written over each other. Each of the three tests below is one of the
    reasons, and none of them is about the data being wrong — the archive was
    fine, the picture was not.
    """

    def test_the_map_sizes_by_how_much_a_collection_holds(self) -> None:
        """``uniform`` is honest on a message view, where nothing may be scored
        yet, and useless on the map: ``message_count`` is written by every
        rebuild there has ever been, so a map that ignores it draws seventy-five
        identical circles over an archive that is nothing like uniform."""
        size_by, _ = defaults_for(GraphView.OVERVIEW)

        assert size_by is SizeBy.COUNT

    def test_the_map_is_drawn_as_rings_and_not_as_a_force(self) -> None:
        """``cose`` lays out *forces along edges*. Handed a set with no edges it
        degenerates to a lattice, which is exactly the grid of dots that was
        reported. Concentric ranks by weight instead, so the biggest collection
        lands in the middle and the tail goes to the rim."""
        _, layout = defaults_for(GraphView.OVERVIEW)

        assert layout is LayoutName.CONCENTRIC

    def test_a_rooted_view_keeps_making_no_claim_about_size(self) -> None:
        """Only the map changes. A topic or a message view is small enough to
        label completely and may have nothing scored yet, so its default stays
        the one that does not assert a size it cannot back."""
        for view in (GraphView.TOPIC, GraphView.MESSAGE, GraphView.ADDRESS):
            assert defaults_for(view) == (SizeBy.UNIFORM, LayoutName.COSE)

    def test_a_crowded_canvas_labels_only_the_nodes_worth_reading(self) -> None:
        """Above :data:`LABEL_DENSITY_LIMIT` nodes the labels collide into an
        unreadable smear, which loses *every* name rather than the small ones.
        The heaviest :data:`LABELLED_MAX` keep theirs and the tail is drawn
        unlabelled."""
        drawn = _nodes(elements_of(_crowd(75), size_by=SizeBy.COUNT))
        labelled = [one for one in drawn.values() if one["label"]]

        assert len(labelled) == LABELLED_MAX
        assert {one["id"] for one in labelled} == {
            f"topic:{index:03d}" for index in range(LABELLED_MAX)
        }

    def test_an_uncrowded_canvas_labels_everything(self) -> None:
        """The cut is for the smear and nothing else, so a picture that fits its
        names keeps all of them."""
        drawn = _nodes(elements_of(_crowd(LABEL_DENSITY_LIMIT), size_by=SizeBy.COUNT))

        assert all(one["label"] for one in drawn.values())

    def test_dropping_a_label_never_drops_the_node_or_its_card(self) -> None:
        """The tail is still on the canvas, still the right size, still
        clickable — and the details column names it, because ``card_of`` reads
        the subgraph rather than the element."""
        crowd = _crowd(75)
        drawn = _nodes(elements_of(crowd, size_by=SizeBy.COUNT))

        assert len(drawn) == 75
        assert drawn["topic:074"]["weight"] > 0
        assert card_of(crowd.nodes[74]).title == "a topic called number 74"

    def test_the_hidden_labels_are_the_light_ones_whatever_the_order(self) -> None:
        """Sorted by weight and not by arrival: the reader must not have to
        know what order a statement returned its rows in."""
        crowd = _crowd(75)
        shuffled = Subgraph(nodes=tuple(reversed(crowd.nodes)), edges=())

        assert {
            one["id"]
            for one in _nodes(elements_of(shuffled, size_by=SizeBy.COUNT)).values()
            if one["label"]
        } == {
            one["id"]
            for one in _nodes(elements_of(crowd, size_by=SizeBy.COUNT)).values()
            if one["label"]
        }
