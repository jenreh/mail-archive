"""What the explorer asks the graph for, and what it draws out of the answer.

The projection itself — a subgraph into elements, a stylesheet, a layout — is
next door in ``test_ui_graph_model`` and needs no state at all. What is left
here is everything that is only true of the *page*: that a link's query
parameters decide what is drawn, that a stale cluster id says so instead of
showing an empty canvas (R7), that picking a message opens it in the reading
pane, that a double-click lays one hop over the picture rather than replacing
it, and that changing how nodes are sized redraws them without asking the graph
anything.

The reader is a fake in the service registry, snapshot-and-restored like the
insights tests do it: what is under test is the state, and a real
``GraphReader`` would put a graph server in the middle of it.
"""

import inspect
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import reflex as rx
from appkit_commons.registry import service_registry
from reflex.istate.data import ReflexURL, RouterData

from mailarc_analytics import (
    AnalyticsReader,
    CoAddressedRow,
    CommunityRow,
    GraphEdge,
    GraphNode,
    NodeKind,
    Subgraph,
    TopicRow,
)
from mailarc_analytics.queries.graphs import GraphReader
from mailarc_core.archive import ArchiveReader, MessageSummary, TagStore, TagSummary
from mailarc_ui.graph import (
    DARK_STYLESHEET,
    LIGHT_STYLESHEET,
    GraphExplorerState,
    GraphView,
    LayoutName,
    NodeCard,
    SizeBy,
    explorer_panel,
    picker_options,
)
from mailarc_ui.shell import routes
from mailarc_ui.theme import Palette

MARCH = datetime(2026, 3, 12, 9, 0, tzinfo=UTC)

RAW = b"From: anna@example.com\r\nSubject: Nord 42\r\n\r\nThe kick-off is Monday.\r\n"
"""One real message, so the reading pane parses rather than being stubbed."""


def _message(node_id: str, **weights: float) -> GraphNode:
    return GraphNode(
        id=node_id,
        kind=NodeKind.MESSAGE,
        label=f"Subject of {node_id}",
        weights=dict(weights),
    )


def _address(node_id: str, **weights: float) -> GraphNode:
    return GraphNode(id=node_id, kind=NodeKind.ADDRESS, label=node_id, weights=weights)


class FakeGraphReader:
    """Every view, scripted per root id, recording what it was asked.

    ``asked`` is the assertion that matters most in this file: re-sizing,
    re-laying-out and hiding a kind are all redraws of a picture the page
    already holds, and a handler that quietly re-reads the graph to do one of
    them is a page that stutters on every dropdown.
    """

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], Subgraph] = {}
        self.asked: list[tuple[str, str]] = []
        self.failing = False

    def plant(self, view: str, root: str, found: Subgraph) -> None:
        self.answers[(view, root)] = found

    def _answer(self, view: str, root: str) -> Subgraph:
        if self.failing:
            raise ConnectionError("graph is down")
        self.asked.append((view, root))
        return self.answers.get((view, root), Subgraph())

    def overview(self, *, limit: int = 200) -> Subgraph:
        return self._answer("overview", "")

    def message(self, message_id: str, *, depth: int = 1, limit: int = 200) -> Subgraph:
        return self._answer("message", message_id)

    def topic(self, topic_id: str, *, limit: int = 200) -> Subgraph:
        return self._answer("topic", topic_id)

    def address(self, address_id: str, *, limit: int = 200) -> Subgraph:
        return self._answer("address", address_id)

    def tag(self, tag_id: str, *, limit: int = 200) -> Subgraph:
        return self._answer("tag", tag_id)

    def community(self, community_id: str, *, limit: int = 200) -> Subgraph:
        return self._answer("community", community_id)

    def expand(self, node_id: str, kind: NodeKind, *, limit: int = 200) -> Subgraph:
        return self._answer(f"expand:{kind.value}", node_id)

    def path(self, left: str, right: str, *, max_len: int = 4) -> Subgraph:
        return self._answer("path", f"{left}|{right}")


class FakeArchive:
    """The two reads the reading pane makes, and the picker's listing."""

    def __init__(self) -> None:
        self.summaries: dict[str, MessageSummary] = {}
        self.blobs: dict[str, bytes] = {}

    def plant(self, message_id: str, *, digest: str = "") -> None:
        self.summaries[message_id] = MessageSummary(
            id=message_id,
            sender_address="anna@example.com",
            subject=f"Subject of {message_id}",
            sent_at=MARCH,
            eml_sha256=digest or None,
        )
        if digest:
            self.blobs[digest] = RAW

    def list_messages(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[MessageSummary]:
        return list(self.summaries.values())[offset : offset + limit]

    def messages_by_ids(self, ids: list[str]) -> list[MessageSummary]:
        return [self.summaries[one] for one in ids if one in self.summaries]

    def raw_message(self, digest: str) -> bytes | None:
        return self.blobs.get(digest)

    def remote_content_trusted(self, address: str) -> bool:
        return False


class FakeAnalytics:
    """The listings the pickers are filled from."""

    def __init__(self) -> None:
        self.topic_rows: tuple[TopicRow, ...] = ()
        self.community_rows: tuple[CommunityRow, ...] = ()
        self.pairs: tuple[CoAddressedRow, ...] = ()

    def topics(self, *, limit: int = 20) -> tuple[TopicRow, ...]:
        return self.topic_rows[:limit]

    def communities(self, *, limit: int = 20) -> tuple[CommunityRow, ...]:
        return self.community_rows[:limit]

    def suggestion_counts(self) -> dict[str, int]:
        return {}

    def suggestions_for(self, tag_id: str, *, limit: int = 20) -> tuple[Any, ...]:
        return ()

    def top_co_addressed(self, *, limit: int = 20) -> tuple[CoAddressedRow, ...]:
        return self.pairs[:limit]


class FakeTags:
    """Enough of the annotation layer for the tag picker and the mixin's load."""

    def __init__(self) -> None:
        self.summaries: list[TagSummary] = []

    def list_tags(self) -> tuple[TagSummary, ...]:
        return tuple(self.summaries)

    def tags_of(self, ids: Sequence[str]) -> dict[str, tuple[TagSummary, ...]]:
        return dict.fromkeys(ids, ())


@pytest.fixture
def reader() -> FakeGraphReader:
    return FakeGraphReader()


@pytest.fixture
def archive() -> FakeArchive:
    return FakeArchive()


@pytest.fixture
def analytics() -> FakeAnalytics:
    return FakeAnalytics()


@pytest.fixture
def tags() -> FakeTags:
    return FakeTags()


@pytest.fixture
def published(
    reader: FakeGraphReader,
    archive: FakeArchive,
    analytics: FakeAnalytics,
    tags: FakeTags,
) -> Iterator[FakeGraphReader]:
    """Everything the composition root publishes for this page, faked."""
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(GraphReader, cast(GraphReader, reader))
    registry.register_as(ArchiveReader, cast(ArchiveReader, archive))
    registry.register_as(AnalyticsReader, cast(AnalyticsReader, analytics))
    registry.register_as(TagStore, cast(TagStore, tags))
    yield reader
    registry.restore(saved)


@pytest.fixture
def root(published: FakeGraphReader) -> rx.State:
    return rx.State()


@pytest.fixture
def state(root: rx.State) -> GraphExplorerState:
    return cast(
        GraphExplorerState,
        root.get_substate(GraphExplorerState.get_full_name().split(".")[1:]),
    )


def _arrive_at(root: rx.State, query: str) -> None:
    """Put the browser on ``/graph`` with these query parameters.

    ``router`` is inherited from the root state, so it is set there — which is
    also where Reflex itself sets it.
    """
    root.router = RouterData(url=ReflexURL(f"http://localhost{routes.GRAPH}?{query}"))


async def _fire(handler: Any, state: GraphExplorerState, *args: Any) -> None:
    """Run one handler the way Reflex runs it — through its wrapped function.

    Both shapes: a redraw is ordinary and a read is a coroutine, and the point
    of this file is partly that the first kind never becomes the second.
    """
    found = handler.fn(state, *args)
    if inspect.isawaitable(found):
        await found


def _ids(state: GraphExplorerState) -> set[str]:
    return {one["data"]["id"] for one in state.elements if one["group"] == "nodes"}


def _weight(state: GraphExplorerState, node_id: str) -> float:
    return next(
        one["data"]["weight"]
        for one in state.elements
        if one["group"] == "nodes" and one["data"]["id"] == node_id
    )


class TestWhatALinkAsksFor:
    async def test_the_query_parameters_decide_what_is_drawn(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> None:
        reader.plant(
            "topic",
            "topic:abc",
            Subgraph(nodes=(_message("m1"), _address("anna@example.com"))),
        )
        _arrive_at(root, "view=topic&id=topic:abc")

        await _fire(GraphExplorerState.load, state)

        assert state.view_name == GraphView.TOPIC.value
        assert state.picked_id == "topic:abc"
        assert reader.asked == [("topic", "topic:abc")]
        assert _ids(state) == {"m1", "anna@example.com"}

    async def test_a_link_with_no_parameters_draws_the_overview(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> None:
        reader.plant("overview", "", Subgraph(nodes=(_message("m1"),)))
        _arrive_at(root, "")

        await _fire(GraphExplorerState.load, state)

        assert state.view_name == GraphView.OVERVIEW.value
        assert reader.asked == [("overview", "")]

    async def test_a_view_nobody_serves_falls_back_rather_than_raising(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> None:
        """A bookmark outlives a rename, and a 500 is a worse answer than the
        map."""
        _arrive_at(root, "view=constellation&id=x")

        await _fire(GraphExplorerState.load, state)

        assert state.view_name == GraphView.OVERVIEW.value

    async def test_a_topic_that_no_longer_resolves_says_to_pick_again(
        self, state: GraphExplorerState, root: rx.State
    ) -> None:
        """R7: a topic id is a digest of its members and is minted afresh by
        every rebuild, so a bookmarked link goes stale by design."""
        _arrive_at(root, "view=topic&id=topic:gone")

        await _fire(GraphExplorerState.load, state)

        assert "recomputed" in state.notice
        assert "pick it again" in state.notice
        assert state.elements == []

    async def test_a_graph_that_went_away_is_a_sentence_not_an_exception(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> None:
        reader.failing = True
        _arrive_at(root, "")

        await _fire(GraphExplorerState.load, state)

        assert "graph is down" in state.error
        assert state.loading is False

    async def test_the_picture_says_when_it_is_only_part_of_the_answer(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> None:
        reader.plant(
            "overview",
            "",
            Subgraph(nodes=(_message("m1"),), truncated=True, notice="200 of more"),
        )
        _arrive_at(root, "")

        await _fire(GraphExplorerState.load, state)

        assert state.truncated_notice == "200 of more"


class TestChangingTheView:
    async def test_choosing_a_view_clears_the_root_and_fills_the_picker(
        self,
        state: GraphExplorerState,
        analytics: FakeAnalytics,
        reader: FakeGraphReader,
    ) -> None:
        analytics.community_rows = (
            CommunityRow(id="community:a", label="kunde.example", size=4),
        )
        state.picked_id = "topic:abc"

        await _fire(GraphExplorerState.choose_view, state, GraphView.COMMUNITY.value)

        assert state.picked_id == ""
        assert state.options == [{"value": "community:a", "label": "kunde.example (4)"}]
        assert reader.asked == [], "nothing to draw until a circle is picked"

    async def test_picking_a_root_reads_that_view(
        self, state: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        reader.plant("tag", "tag:nord-42", Subgraph(nodes=(_message("m1"),)))
        state.view_name = GraphView.TAG.value

        await _fire(GraphExplorerState.pick, state, "tag:nord-42")

        assert reader.asked == [("tag", "tag:nord-42")]
        assert state.picked_id == "tag:nord-42"


class TestRedrawingWithoutAsking:
    @pytest.fixture
    async def drawn(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> GraphExplorerState:
        reader.plant(
            "overview",
            "",
            Subgraph(
                nodes=(
                    _message("m1", importance=1.0, degree=0.5),
                    _message("m2", degree=1.0),
                    _address("anna@example.com", degree=0.25),
                ),
                edges=(GraphEdge(source="m1", target="anna@example.com", kind="SENT"),),
            ),
        )
        _arrive_at(root, "")
        await _fire(GraphExplorerState.load, state)
        reader.asked.clear()
        return state

    async def test_sizing_by_something_else_reweights_the_same_picture(
        self, drawn: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        await _fire(GraphExplorerState.set_size_by, drawn, SizeBy.IMPORTANCE.value)

        assert reader.asked == [], "the subgraph is already here"
        assert _weight(drawn, "m1") == pytest.approx(1.0)

    async def test_a_node_with_no_such_number_is_drawn_in_the_middle(
        self, drawn: GraphExplorerState
    ) -> None:
        """Not the smallest circle: the smallest circle is a claim, and it
        would be a false one."""
        await _fire(GraphExplorerState.set_size_by, drawn, SizeBy.IMPORTANCE.value)

        assert _weight(drawn, "m2") == pytest.approx(0.5)

    async def test_hiding_a_kind_takes_its_edges_with_it(
        self, drawn: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        """Cytoscape throws on an edge whose end is not in the collection, and
        the throw aborts the whole batch — one filtered node empties the
        canvas."""
        await _fire(GraphExplorerState.toggle_kind, drawn, NodeKind.ADDRESS.value)

        assert _ids(drawn) == {"m1", "m2"}
        assert [one for one in drawn.elements if one["group"] == "edges"] == []
        assert reader.asked == []

    async def test_toggling_a_kind_twice_brings_it_back(
        self, drawn: GraphExplorerState
    ) -> None:
        await _fire(GraphExplorerState.toggle_kind, drawn, NodeKind.ADDRESS.value)
        await _fire(GraphExplorerState.toggle_kind, drawn, NodeKind.ADDRESS.value)

        assert drawn.hidden_kinds == []
        assert "anna@example.com" in _ids(drawn)

    async def test_changing_the_layout_changes_only_the_layout(
        self, drawn: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        await _fire(GraphExplorerState.set_layout, drawn, LayoutName.CONCENTRIC.value)

        assert drawn.layout["name"] == "concentric"
        assert reader.asked == []

    async def test_fitting_bumps_the_token_the_canvas_watches(
        self, drawn: GraphExplorerState
    ) -> None:
        """A counter and not a flag: fitting a picture that is already fit has
        to fit it again, and there is no other state change to observe."""
        before = drawn.fit_token

        await _fire(GraphExplorerState.fit, drawn)

        assert drawn.fit_token == before + 1

    async def test_a_depth_typed_as_text_is_still_a_number(
        self, drawn: GraphExplorerState
    ) -> None:
        """``mn.number_input`` hands over ``float | str`` — an emptied box
        arrives as ``""``."""
        await _fire(GraphExplorerState.set_depth, drawn, "")

        assert drawn.depth == 1


class TestPickingANode:
    @pytest.fixture
    async def drawn(
        self,
        state: GraphExplorerState,
        root: rx.State,
        reader: FakeGraphReader,
        archive: FakeArchive,
    ) -> GraphExplorerState:
        archive.plant("m1", digest="d1")
        reader.plant(
            "overview",
            "",
            Subgraph(nodes=(_message("m1"), _address("anna@example.com"))),
        )
        _arrive_at(root, "")
        await _fire(GraphExplorerState.load, state)
        reader.asked.clear()
        return state

    async def test_picking_a_message_opens_it_in_the_reading_pane(
        self, drawn: GraphExplorerState
    ) -> None:
        await _fire(GraphExplorerState.select_node, drawn, "m1")

        assert drawn.selected_node.kind == NodeKind.MESSAGE.value
        assert drawn.selected_id == "m1"
        assert "kick-off" in drawn.view.body_text

    async def test_picking_an_address_names_it_without_opening_anything(
        self, drawn: GraphExplorerState
    ) -> None:
        await _fire(GraphExplorerState.select_node, drawn, "anna@example.com")

        assert drawn.selected_node.kind == NodeKind.ADDRESS.value
        assert drawn.selected_id == "", "there is no message to read"

    async def test_a_node_that_is_not_in_the_picture_is_ignored(
        self, drawn: GraphExplorerState
    ) -> None:
        await _fire(GraphExplorerState.select_node, drawn, "nothing")

        assert drawn.selected_node.id == ""

    async def test_clearing_the_selection_empties_the_card(
        self, drawn: GraphExplorerState
    ) -> None:
        await _fire(GraphExplorerState.select_node, drawn, "anna@example.com")

        await _fire(GraphExplorerState.clear_selection, drawn)

        assert drawn.selected_node.id == ""


class TestExpandingANode:
    @pytest.fixture
    async def drawn(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> GraphExplorerState:
        reader.plant("overview", "", Subgraph(nodes=(_address("anna@example.com"),)))
        _arrive_at(root, "")
        await _fire(GraphExplorerState.load, state)
        reader.asked.clear()
        return state

    async def test_one_hop_is_laid_over_the_picture_rather_than_replacing_it(
        self, drawn: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        reader.plant(
            "expand:address",
            "anna@example.com",
            Subgraph(
                nodes=(_address("anna@example.com"), _message("m7")),
                edges=(
                    GraphEdge(source="m7", target="anna@example.com", kind="SENT_TO"),
                ),
            ),
        )

        await _fire(GraphExplorerState.expand_node, drawn, "anna@example.com")

        assert reader.asked == [("expand:address", "anna@example.com")]
        assert _ids(drawn) == {"anna@example.com", "m7"}

    async def test_expanding_a_node_the_picture_does_not_hold_asks_nothing(
        self, drawn: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        await _fire(GraphExplorerState.expand_node, drawn, "nothing")

        assert reader.asked == []

    async def test_a_route_between_two_correspondents_is_merged_in(
        self, drawn: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        reader.plant(
            "path",
            "anna@example.com|thomas@example.com",
            Subgraph(
                nodes=(_address("anna@example.com"), _address("thomas@example.com")),
                edges=(
                    GraphEdge(
                        source="anna@example.com",
                        target="thomas@example.com",
                        kind="CO_ADDRESSED",
                    ),
                ),
            ),
        )
        drawn.picked_id = "anna@example.com"

        await _fire(GraphExplorerState.show_path, drawn, "thomas@example.com")

        assert "thomas@example.com" in _ids(drawn)


class TestEveryViewIsServed:
    """One test for the whole table, because the failure it catches is a view
    that quietly reads a different one and draws somebody else's answer."""

    @pytest.mark.parametrize(
        ("view", "rooted_at"),
        [
            (GraphView.MESSAGE, "m1"),
            (GraphView.TOPIC, "topic:abc"),
            (GraphView.ADDRESS, "anna@example.com"),
            (GraphView.TAG, "tag:nord-42"),
            (GraphView.COMMUNITY, "community:abc"),
        ],
    )
    async def test_a_view_asks_the_reader_for_its_own_answer(
        self,
        state: GraphExplorerState,
        reader: FakeGraphReader,
        view: GraphView,
        rooted_at: str,
    ) -> None:
        state.view_name = view.value

        await _fire(GraphExplorerState.pick, state, rooted_at)

        assert reader.asked == [(view.value, rooted_at)]

    async def test_a_circle_that_no_longer_resolves_says_to_pick_again(
        self, state: GraphExplorerState
    ) -> None:
        """A community id is a digest of its members too, so R7 is about both."""
        state.view_name = GraphView.COMMUNITY.value

        await _fire(GraphExplorerState.pick, state, "community:gone")

        assert "circle was recomputed" in state.notice

    async def test_a_tag_with_nothing_left_on_it_says_only_that(
        self, state: GraphExplorerState
    ) -> None:
        """A tag id is durable, so "pick it again" would be wrong advice."""
        state.view_name = GraphView.TAG.value

        await _fire(GraphExplorerState.pick, state, "tag:empty")

        assert "recomputed" not in state.notice
        assert state.notice != ""

    async def test_a_view_with_no_root_yet_asks_nothing_and_says_so(
        self, state: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        await _fire(GraphExplorerState.choose_view, state, GraphView.TOPIC.value)

        assert reader.asked == []
        assert "Pick something" in state.notice

    async def test_promoting_a_circle_reads_the_circle(
        self, state: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        reader.plant("community", "community:abc", Subgraph(nodes=(_message("m1"),)))

        assert await state._cluster_members("community", "community:abc") == ("m1",)


class TestThePickerOptions:
    def test_a_topic_is_offered_once_however_many_signals_drew_it(
        self, published: FakeGraphReader, analytics: FakeAnalytics
    ) -> None:
        """``AnalyticsReader.topics`` is one row per topic *per signal*, and a
        dropdown with the same topic in it three times is a dropdown nobody
        can pick from."""
        analytics.topic_rows = (
            TopicRow(id="topic:a", label="rechnung", method="ref", messages=6),
            TopicRow(id="topic:a", label="rechnung", method="subject", messages=2),
        )

        assert picker_options(GraphView.TOPIC) == [
            {"value": "topic:a", "label": "rechnung (6)"}
        ]

    def test_the_newest_mail_is_what_a_message_view_offers(
        self, published: FakeGraphReader, archive: FakeArchive
    ) -> None:
        archive.plant("m1")

        assert picker_options(GraphView.MESSAGE) == [
            {"value": "m1", "label": "Subject of m1"}
        ]

    def test_a_person_is_offered_once_however_many_pairs_name_them(
        self, published: FakeGraphReader, analytics: FakeAnalytics
    ) -> None:
        """The picker is the correspondents worth starting at, and the pairs
        are what names them — so anna, on both, is one entry."""
        analytics.pairs = (
            CoAddressedRow(
                left_id="anna@example.com", right_id="bob@example.com", together=5
            ),
            CoAddressedRow(
                left_id="anna@example.com", right_id="carl@example.com", together=3
            ),
        )

        assert [one["value"] for one in picker_options(GraphView.ADDRESS)] == [
            "anna@example.com",
            "bob@example.com",
            "carl@example.com",
        ]

    def test_a_tag_is_offered_with_what_it_holds(
        self, published: FakeGraphReader, tags: FakeTags
    ) -> None:
        tags.summaries = [TagSummary(id="tag:nord-42", name="nord-42")]

        assert picker_options(GraphView.TAG) == [
            {"value": "tag:nord-42", "label": "nord-42 (0)"}
        ]

    def test_the_overview_needs_nothing_picked(
        self, published: FakeGraphReader
    ) -> None:
        assert picker_options(GraphView.OVERVIEW) == []

    def test_a_listing_that_failed_is_an_empty_picker_and_not_a_crash(
        self, published: FakeGraphReader
    ) -> None:
        registry = service_registry()
        saved = registry.snapshot()
        try:
            registry.restore({})
            assert picker_options(GraphView.TAG) == []
        finally:
            registry.restore(saved)


class TestWhatTheColumnsAsk:
    """The four computed vars the components branch on, each on its own.

    Rendered branches are invisible in every other test in this file: a page
    that showed the reading pane for an address, or offered a promote form for
    a person, would draw perfectly and be wrong.
    """

    def test_the_map_needs_no_root_and_every_other_view_does(
        self, state: GraphExplorerState
    ) -> None:
        assert state.needs_a_root is False

        state.view_name = GraphView.TAG.value

        assert state.needs_a_root is True

    def test_a_picture_is_something_drawn(self, state: GraphExplorerState) -> None:
        assert state.has_picture is False

        state.elements = [{"group": "nodes", "data": {"id": "m1"}}]

        assert state.has_picture is True

    def test_only_a_message_opens_the_reading_pane(
        self, state: GraphExplorerState
    ) -> None:
        state.selected_node = NodeCard(id="m1", kind=NodeKind.MESSAGE.value)
        assert state.is_message is True
        assert state.is_cluster is False

    def test_a_topic_and_a_circle_are_the_two_things_that_become_tags(
        self, state: GraphExplorerState
    ) -> None:
        for kind in (NodeKind.TOPIC, NodeKind.COMMUNITY):
            state.selected_node = NodeCard(id="x", kind=kind.value)
            assert state.is_cluster is True

        state.selected_node = NodeCard(id="a@b.c", kind=NodeKind.ADDRESS.value)

        assert state.is_cluster is False


class TestPickingATagNode:
    async def test_picking_a_tag_opens_what_it_is_being_offered(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> None:
        """A tag is the one node whose details are a listing rather than
        properties, and it is read when the node is picked rather than behind a
        second click."""
        reader.plant(
            "overview",
            "",
            Subgraph(
                nodes=(GraphNode(id="tag:nord-42", kind=NodeKind.TAG, label="nord-42"),)
            ),
        )
        _arrive_at(root, "")
        await _fire(GraphExplorerState.load, state)

        await _fire(GraphExplorerState.select_node, state, "tag:nord-42")

        assert state.suggestion_tag == "tag:nord-42"
        assert state.selected_id == "", "a tag is not something to read"


class TestTheDepthOnlyRereadsWhereItMatters:
    async def test_a_message_view_reads_again_at_the_new_depth(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> None:
        reader.plant("message", "m1", Subgraph(nodes=(_message("m1"),)))
        _arrive_at(root, "view=message&id=m1")
        await _fire(GraphExplorerState.load, state)
        reader.asked.clear()

        await _fire(GraphExplorerState.set_depth, state, 2)

        assert state.depth == 2
        assert reader.asked == [("message", "m1")]

    async def test_a_depth_past_the_cap_is_the_cap(
        self, state: GraphExplorerState
    ) -> None:
        """Three is what the reply-chain statement walks, and a variable-length
        quantifier is Cypher syntax rather than a bound value."""
        await _fire(GraphExplorerState.set_depth, state, 99)

        assert state.depth == 3


class TestAskingForARoute:
    async def test_two_people_with_nothing_between_them_are_told_so(
        self, state: GraphExplorerState, root: rx.State, reader: FakeGraphReader
    ) -> None:
        reader.plant("overview", "", Subgraph(nodes=(_address("anna@example.com"),)))
        _arrive_at(root, "")
        await _fire(GraphExplorerState.load, state)
        state.picked_id = "anna@example.com"

        await _fire(GraphExplorerState.show_path, state, "stranger@example.com")

        assert "Nothing connects" in state.tag_notice

    async def test_a_route_from_a_node_to_itself_is_not_asked_for(
        self, state: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        state.picked_id = "anna@example.com"

        await _fire(GraphExplorerState.show_path, state, "anna@example.com")

        assert reader.asked == []


class TestPromotingWhatIsOnTheCanvas:
    """The explorer's half of the tag actions: which messages a tag is made of."""

    async def test_a_cluster_is_promoted_over_the_whole_topic_not_the_drawing(
        self, state: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        """Read afresh and at the member limit, because a picture is capped and
        half a project is a worse tag than none."""
        reader.plant(
            "topic",
            "topic:abc",
            Subgraph(nodes=(_message("m1"), _message("m2"), _address("a@b.c"))),
        )

        found = await state._cluster_members("topic", "topic:abc")

        assert found == ("m1", "m2"), "only the mail, and all of it"

    async def test_nothing_is_promoted_from_something_that_is_not_a_cluster(
        self, state: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        assert await state._cluster_members("address", "a@b.c") == ()
        assert reader.asked == []

    async def test_a_graph_that_went_away_leaves_the_members_empty(
        self, state: GraphExplorerState, reader: FakeGraphReader
    ) -> None:
        """Which is what the mixin refuses to promote, so no empty tag is made."""
        reader.failing = True

        assert await state._cluster_members("topic", "topic:abc") == ()
        assert "graph is down" in state.error


class TestTheWaysIntoTheExplorer:
    """Both links that arrive here, asserted where the explorer is documented.

    Read off the render rather than off the source: a constant nobody puts in a
    component is a route nothing links to, and neither module's own tests look
    at where its pills point.
    """

    def test_a_topic_on_the_insights_page_offers_the_graph(self) -> None:
        from mailarc_ui.insights.components import topics_card

        assert f"{routes.GRAPH}?view=topic&id=" in json.dumps(
            topics_card().render(), default=str
        )

    def test_a_search_hit_offers_to_be_shown_in_the_graph(self) -> None:
        """Through the list rather than through one row: a result row is drawn
        inside an ``rx.foreach`` and only takes a ``Var`` there."""
        from mailarc_ui.search.components import result_list

        assert f"{routes.GRAPH}?view=message&id=" in json.dumps(
            result_list().render(), default=str
        )


class TestThePanelDraws:
    def test_the_explorer_panel_builds(self, published: FakeGraphReader) -> None:
        """A prop appkit_mantine does not have only shows up when it is built."""
        assert isinstance(explorer_panel(), rx.Component)


class TestTheCanvasIsPaintedForBothSchemes:
    """The one path by which a colour scheme reaches a ``<canvas>``.

    ``elements_of`` bakes the *light* hex into every element's ``data(color)``
    — the elements are the biggest var on the page and the picture is the same
    picture in both schemes, so they must not change when the scheme does. What
    changes instead is the stylesheet: ``_sheet`` appends one rule per kind, and
    ``graph/components.py`` picks between the two with ``rx.color_mode_cond``.

    Two facts hold that up and neither is visible in a browser when it breaks.
    A missing per-kind rule leaves a dark canvas wearing the light palette, and
    a per-kind rule placed *before* the base ``node`` rule loses to it —
    cytoscape resolves a tie by document order — which is the same wrong picture
    from the other direction. Both are silent: no error, no console line.
    """

    def _kind_rules(self, sheet: list[dict[str, Any]]) -> dict[str, str]:
        return {
            one["selector"]: one["style"]["background-color"]
            for one in sheet
            if one["selector"].startswith("node[kind")
        }

    def test_each_scheme_colours_every_kind_from_its_own_palette(self) -> None:
        for sheet, palette in (
            (LIGHT_STYLESHEET, Palette.LIGHT),
            (DARK_STYLESHEET, Palette.DARK),
        ):
            found = self._kind_rules(sheet)

            assert found == {
                f'node[kind = "{one.value}"]': palette[one.value] for one in NodeKind
            }

    def test_the_two_schemes_do_not_draw_the_same_canvas(self) -> None:
        assert self._kind_rules(LIGHT_STYLESHEET) != self._kind_rules(DARK_STYLESHEET)

    def test_a_kind_rule_comes_after_the_fill_it_has_to_beat(self) -> None:
        """Later wins, so every per-kind rule sits behind the base ``node`` one."""
        for sheet in (LIGHT_STYLESHEET, DARK_STYLESHEET):
            base = [at for at, one in enumerate(sheet) if one["selector"] == "node"]
            kinds = [
                at
                for at, one in enumerate(sheet)
                if one["selector"].startswith("node[kind")
            ]

            assert base
            assert kinds
            assert min(kinds) > max(base)

    def test_both_stylesheets_reach_the_page(self, published: FakeGraphReader) -> None:
        """``rx.color_mode_cond`` is the wiring, and it is only wiring once both
        arms are in what the page renders."""
        drawn = json.dumps(explorer_panel().render(), default=str)

        assert Palette.LIGHT[NodeKind.MESSAGE.value] in drawn
        assert Palette.DARK[NodeKind.MESSAGE.value] in drawn
