"""The reading pane's third tab: a chip pressed, and the corner it draws.

Two halves and one seam between them. The pane records which chip was pressed
and knows nothing about graphs (``test_ui_message_detail``); this mixin is what
fills the hook it leaves open, so what is under test here is the *pair* — that
a topic chip becomes a picture, that a group chip cannot ask for one, and that
the two sets never drift apart.

The reader is a fake in the service registry, snapshot-and-restored the way
``test_ui_graph_state`` does it: a real ``GraphReader`` would put a graph
server in the middle of a projection test.
"""

from collections.abc import Callable, Iterator
from typing import cast

import pytest
import reflex as rx
from appkit_commons.registry import service_registry

from mailarc_analytics import GraphEdge, GraphNode, NodeKind, Subgraph
from mailarc_analytics.queries.graphs import GraphReader
from mailarc_ui.graph import CHIP_LIMIT, NOTHING_TO_DRAW, ChipGraphState
from mailarc_ui.graph.chips import _PICTURES, _READERS, chip_graph_panel
from mailarc_ui.message_detail import DRAWABLE_CHIPS, TAB_GRAPH, ChipKind
from mailarc_ui.shell import routes


class OtherChipState(ChipGraphState, rx.State):
    """A page the application does not have — the argument for the mixin.

    Empty on purpose: whatever it can do, it got from the two mixins behind it.
    """


class FakeGraphReader:
    """Every view the header can open, scripted per root, recording the ask."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], Subgraph] = {}
        self.asked: list[tuple[str, str, int]] = []
        self.failing = False
        self.depths: list[int] = []
        self.while_reading: Callable[[], None] | None = None
        """Run in the middle of a read — what a second chip pressed while the
        first was still in flight looks like from here."""

    def plant(self, view: str, root: str, found: Subgraph) -> None:
        self.answers[(view, root)] = found

    def _answer(self, view: str, root: str, limit: int) -> Subgraph:
        if self.failing:
            raise ConnectionError("graph is down")
        self.asked.append((view, root, limit))
        if self.while_reading is not None:
            self.while_reading()
        return self.answers.get((view, root), Subgraph())

    def topic(self, topic_id: str, *, limit: int = 200) -> Subgraph:
        return self._answer("topic", topic_id, limit)

    def tag(self, tag_id: str, *, limit: int = 200) -> Subgraph:
        return self._answer("tag", tag_id, limit)

    def address(self, address_id: str, *, limit: int = 200) -> Subgraph:
        return self._answer("address", address_id, limit)

    def message(self, message_id: str, *, depth: int = 1, limit: int = 200) -> Subgraph:
        self.depths.append(depth)
        return self._answer("message", message_id, limit)


def _drawn(hub: str, kind: NodeKind) -> Subgraph:
    """A hub and one message on it — the smallest picture that is one."""
    return Subgraph(
        nodes=[
            GraphNode(id=hub, kind=kind, label="Nord 42"),
            GraphNode(id="m1", kind=NodeKind.MESSAGE, label="The kick-off"),
        ],
        edges=[GraphEdge(source="m1", target=hub, kind="ABOUT")],
    )


@pytest.fixture
def reader() -> FakeGraphReader:
    return FakeGraphReader()


@pytest.fixture
def published(reader: FakeGraphReader) -> Iterator[FakeGraphReader]:
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(GraphReader, cast(GraphReader, reader))
    yield reader
    registry.restore(saved)


@pytest.fixture
def state(published: FakeGraphReader) -> OtherChipState:
    return OtherChipState()


class TestWhatTheMixinPromises:
    def test_it_can_draw_exactly_the_chips_the_header_offers(self) -> None:
        """The two sets, checked against each other. Something the header
        shows as pressable and this cannot read would be a tab that never
        fills; a view here with nothing in front of it would be dead code."""
        assert set(_READERS) == {one.value for one in DRAWABLE_CHIPS}

    def test_every_view_it_names_is_one_it_knows_how_to_read(self) -> None:
        """The second half of the same claim, and the one a new view trips
        over: naming a view in `_READERS` without a reader beside it is a
        `KeyError` the moment somebody presses that glyph."""
        assert set(_PICTURES) == set(_READERS.values())

    def test_it_is_the_reading_pane_with_a_picture_added(self) -> None:
        """A page that lists it gets both halves — the mail and the graph."""
        assert hasattr(OtherChipState, "chips")
        assert hasattr(OtherChipState, "chip_elements")


class TestDrawingAChip:
    async def test_a_topic_chip_becomes_a_picture(self, state, reader) -> None:
        reader.plant("topic", "topic:1", _drawn("topic:1", NodeKind.TOPIC))

        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:1", "Nord 42")

        assert state.tab == TAB_GRAPH
        assert [one["data"]["id"] for one in state.chip_elements] == [
            "topic:1",
            "m1",
            "m1|ABOUT|topic:1",
        ]
        assert state.chip_graph_layout["name"]
        assert state.chip_error == ""
        assert state.chip_notice == ""

    async def test_a_tag_chip_asks_the_other_view(self, state, reader) -> None:
        reader.plant("tag", "t1", _drawn("t1", NodeKind.TAG))

        await state.show_chip_graph(ChipKind.TAG.value, "t1", "Steuer")

        assert reader.asked == [("tag", "t1", CHIP_LIMIT)]

    async def test_a_person_glyph_asks_for_their_own_view(self, state, reader) -> None:
        reader.plant(
            "address", "anna@example.com", _drawn("anna@example.com", NodeKind.ADDRESS)
        )

        await state.show_chip_graph(
            ChipKind.ADDRESS.value, "anna@example.com", "Anna Bauer"
        )

        assert reader.asked == [("address", "anna@example.com", CHIP_LIMIT)]
        assert state.chip_label == "Anna Bauer"

    async def test_a_subject_glyph_asks_for_the_message_one_hop_out(
        self, state, reader
    ) -> None:
        """A glyph beside a subject asks "what is around this mail"; the reply
        chain two hops out is the explorer's question, not the pane's."""
        reader.plant("message", "m1", _drawn("m1", NodeKind.MESSAGE))

        await state.show_chip_graph(ChipKind.MESSAGE.value, "m1", "The kick-off")

        assert reader.asked == [("message", "m1", CHIP_LIMIT)]
        assert reader.depths == [1]

    async def test_it_is_drawn_smaller_than_the_explorer_draws(
        self, state, reader
    ) -> None:
        """A canvas in a column of the reading pane, not a page."""
        reader.plant("topic", "topic:1", _drawn("topic:1", NodeKind.TOPIC))

        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:1", "Nord 42")

        assert reader.asked == [("topic", "topic:1", CHIP_LIMIT)]
        assert CHIP_LIMIT < 200

    async def test_an_id_the_last_rebuild_replaced_says_so(self, state) -> None:
        """R7 in the pane: a topic's id is a digest of its members and every
        rebuild mints it afresh."""
        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:gone", "Nord 42")

        assert state.chip_elements == []
        assert state.chip_notice == NOTHING_TO_DRAW
        assert state.chip_error == ""

    async def test_a_graph_that_went_away_is_a_sentence_not_a_traceback(
        self, state, reader
    ) -> None:
        reader.failing = True

        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:1", "Nord 42")

        assert "graph is down" in state.chip_error
        assert state.chip_elements == []
        assert state.loading_chip is False

    @pytest.mark.parametrize("failing", [True, False])
    async def test_the_canvas_is_never_handed_an_empty_layout(
        self, state, reader, failing
    ) -> None:
        """An empty object is truthy in the browser and cytoscape throws
        "A `name` must be specified to make a layout" on one, which takes the
        whole page down — and the tab is opened *before* the read comes back,
        so a failure leaves the canvas mounted with whatever this holds."""
        reader.failing = failing

        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:1", "Nord 42")

        assert state.chip_graph_layout.get("name")

    async def test_a_group_has_no_view_to_open(self, state, reader) -> None:
        """The header does not offer one as pressable; arriving here anyway
        costs one empty panel rather than an exception."""
        await state.show_chip_graph(ChipKind.GROUP.value, "g1", "5 people")

        assert reader.asked == []
        assert state.chip_notice == NOTHING_TO_DRAW

    async def test_a_second_chip_replaces_the_first(self, state, reader) -> None:
        reader.plant("topic", "topic:1", _drawn("topic:1", NodeKind.TOPIC))
        reader.plant("tag", "t1", _drawn("t1", NodeKind.TAG))

        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:1", "Nord 42")
        await state.show_chip_graph(ChipKind.TAG.value, "t1", "Steuer")

        assert state.chip_label == "Steuer"
        assert state.chip_elements[0]["data"]["id"] == "t1"

    async def test_an_answer_the_next_chip_overtook_is_dropped(
        self, state, reader
    ) -> None:
        """Two chips pressed in quick succession. The first read comes back
        last, and painting it would put one chip's picture under the other
        one's name."""
        reader.plant("topic", "topic:1", _drawn("topic:1", NodeKind.TOPIC))

        def _press_another() -> None:
            state.chip_kind, state.chip_id = ChipKind.TAG.value, "t1"

        reader.while_reading = _press_another
        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:1", "Nord 42")

        assert state.chip_elements == []

    async def test_a_failure_leaves_no_stale_picture_behind(
        self, state, reader
    ) -> None:
        """The canvas is cleared before the read, so an outage shows the
        sentence rather than the previous chip's graph under the new name."""
        reader.plant("topic", "topic:1", _drawn("topic:1", NodeKind.TOPIC))
        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:1", "Nord 42")

        reader.failing = True
        await state.show_chip_graph(ChipKind.TAG.value, "t1", "Steuer")

        assert state.chip_elements == []


class TestThePanel:
    def test_it_builds_on_any_state_that_lists_the_mixin(self) -> None:
        assert isinstance(chip_graph_panel(OtherChipState), rx.Component)

    def test_it_reads_the_state_it_was_handed(self) -> None:
        rendered = str(chip_graph_panel(OtherChipState).render())

        assert OtherChipState.get_full_name().replace(".", "__") in rendered

    def test_it_offers_the_way_into_the_explorer(self) -> None:
        """The same view and root, so the small canvas and the page agree."""
        rendered = str(chip_graph_panel(OtherChipState).render())

        assert f"{routes.GRAPH}?view=" in rendered

    def test_the_canvas_is_painted_in_the_readers_own_scheme(self) -> None:
        """Neither the state nor the projection may hold a colour scheme —
        it is a fact about the browser."""
        rendered = str(chip_graph_panel(OtherChipState).render())

        assert "resolvedColorMode" in rendered
