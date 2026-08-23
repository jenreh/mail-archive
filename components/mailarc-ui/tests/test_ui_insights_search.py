"""Tests for the search panel: both paths, and the state a fresh install is in.

Against the real :class:`~mailarc_analytics.semantic.search.SemanticSearch`
over the fake session in :mod:`insights_archive`, the same way
:mod:`test_ui_insights_state` runs the real ``AnalyticsReader``. Only the
session and the embedder are stand-ins — the first is a server and the second
is an HTTP call to somebody else's process, and neither is what this file is
about.

The claim worth proving here is one sentence of the phase's definition of
done: **with no embedder configured a semantic search produces a clear
message, never an empty result.** An empty list is a valid answer to a search,
so a user who is handed one concludes their archive holds nothing on the
subject — and every hour they then spend not looking again was bought by us
saving an exception. So the no-embedder path is asserted three times over: the
panel says so before anything is pressed, the button that would fail is dead,
and the handler still refuses with the sentence if the event is fired by name.
"""

import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import reflex as rx
from appkit_commons.registry import service_registry
from insights_archive import FakeGraph, FakeUser, signed_in_as
from pydantic import ValidationError
from runic.ogm import Vector

from mailarc_analytics.queries import catalog
from mailarc_analytics.semantic import (
    NO_EMBEDDER,
    NO_FULLTEXT_INDEX,
    EmbedPurpose,
    SearchKind,
    SemanticConfig,
    SemanticSearch,
)
from mailarc_core.archive.reader import GraphSessionFactory
from mailarc_ui.insights import (
    ArchiveSearchState,
    Found,
    HitView,
    archive_search,
    found_summary,
    hits_table,
    insights_panel,
    search_card,
)
from mailarc_ui.insights.model import NO_SUBJECT
from mailarc_ui.insights.search import NOT_ADMIN, NOTHING_ASKED, SEARCH_FAILED

MARCH = datetime(2026, 3, 12, 9, 0, tzinfo=UTC)
DIMENSION = 4
"""Floats per vector here. Four, not the shipped 768: a fixture vector a
reader can see is worth more than a realistic one nobody reads."""

FULLTEXT_COLUMNS = ["id", "subject", "sent_at", "sender", "relevance"]
KNN_COLUMNS = ["id", "subject", "sent_at", "sender", "distance"]
INDEX_COLUMNS = ["label", "properties", "types", "options"]


class StubEmbedder:
    """An embedder that answers instantly and remembers what it was asked.

    Its own five lines rather than an import from ``mailarc-analytics``'s test
    helpers: a component's tests may not reach into another component's, and
    what this file needs of an embedder is that it exists and returns the
    right number of floats.
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self.model = "stub-embed"
        self.dimension = DIMENSION
        self.calls: list[tuple[tuple[str, ...], EmbedPurpose]] = []
        self._error = error

    async def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbedPurpose = EmbedPurpose.DOCUMENT,
    ) -> Sequence[Vector]:
        self.calls.append((tuple(texts), purpose))
        if self._error is not None:
            raise self._error
        return [Vector([1.0, min(len(one), 90) / 100, 0.0, 0.0]) for one in texts]

    async def aclose(self) -> None:
        return None


def _indexes(graph: FakeGraph, *, fulltext: bool = True, vector: int = 0) -> None:
    """Say which indexes this graph carries, the way ``DB.INDEXES`` reports it.

    One row per label with a type per property, because that is the shape the
    store answers in and the shape ``index_options`` has to flatten. Planted
    explicitly in every test that needs it: a graph whose migrations were
    never applied is a state a real installation reaches by upgrading the
    application and not the graph, and both halves of the panel have to say
    so rather than answer with silence.
    """
    types: dict[str, list[str]] = {}
    options: dict[str, dict[str, Any]] = {}
    if fulltext:
        types["subject"] = ["FULLTEXT"]
        types["body_text"] = ["FULLTEXT"]
    if vector:
        types["embedding"] = ["VECTOR"]
        options["embedding"] = {"dimension": vector, "similarityFunction": "cosine"}
    graph.rows(
        catalog.VECTOR_INDEX_OPTIONS,
        INDEX_COLUMNS,
        [["Message", list(types), types, options]],
    )


@pytest.fixture
def archive() -> FakeGraph:
    """A graph with both indexes, two full-text hits and two nearest ones."""
    built = FakeGraph()
    _indexes(built, vector=DIMENSION)
    built.rows(
        catalog.FULLTEXT_MESSAGES,
        FULLTEXT_COLUMNS,
        [
            ["mail-1@example.com", "Rechnung 4711", MARCH.isoformat(), "a@x.de", 4.0],
            ["mail-2@example.com", "", MARCH.isoformat(), "b@x.de", 1.0],
        ],
    )
    built.rows(
        catalog.SEMANTIC_NEIGHBOURS,
        KNN_COLUMNS,
        [
            ["mail-3@example.com", "Invoice 4711", MARCH.isoformat(), "a@x.de", 0.1],
            ["mail-4@example.com", "Zahlungserinnerung", None, "", 0.4],
        ],
    )
    built.rows(catalog.VECTOR_COVERAGE, ["total", "embedded"], [[10, 10]])
    return built


def _factory(graph: FakeGraph) -> GraphSessionFactory:
    """The fake session as the thing a search opens one from."""
    return cast(GraphSessionFactory, lambda: graph)


def _publish(search: SemanticSearch) -> Iterator[SemanticSearch]:
    """Leave a search where the composition root would leave it."""
    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(SemanticSearch, search)
    yield search
    registry.restore(saved)


@pytest.fixture
def offline(archive: FakeGraph) -> Iterator[SemanticSearch]:
    """What a fresh installation has: a search with no embedder behind it."""
    yield from _publish(
        SemanticSearch(_factory(archive), SemanticConfig(), embedder=None)
    )


@pytest.fixture
def embedder() -> StubEmbedder:
    return StubEmbedder()


@pytest.fixture
def online(archive: FakeGraph, embedder: StubEmbedder) -> Iterator[SemanticSearch]:
    """The same archive once somebody configured a model."""
    yield from _publish(
        SemanticSearch(_factory(archive), SemanticConfig(), embedder=embedder)
    )


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> ArchiveSearchState:
    """The panel's state, read by an administrator.

    Signed in on purpose: a search answers with subjects and sender addresses
    out of every mailbox in the installation, and ``_may_read`` refuses
    anybody else. ``TestWhoIsAsking`` is where the refusal is exercised.
    """
    instance = ArchiveSearchState()
    signed_in_as(instance, FakeUser(is_admin=True), monkeypatch)
    return instance


async def _prepare(state: ArchiveSearchState) -> None:
    """The card's ``on_mount``.

    Through the ``EventHandler``'s wrapped function because it is a background
    task and Reflex refuses a direct call on one — the same route
    ``test_ui_insights_state`` takes to ``load``.
    """
    await ArchiveSearchState.prepare.fn(state)  # ty: ignore[unresolved-attribute]


async def _run(state: ArchiveSearchState) -> None:
    """The Search button, same reason as :func:`_prepare`."""
    await ArchiveSearchState.run.fn(state)  # ty: ignore[unresolved-attribute]


async def _search_for(state: ArchiveSearchState, text: str, kind: SearchKind) -> None:
    """Type, pick a path, press Search — what a user does, in one line."""
    state.choose_path(kind.value)
    state.set_query(text)
    await _run(state)


class TestFindingTheSearch:
    def test_the_published_search_is_the_one_the_panel_uses(self, offline) -> None:
        assert archive_search() is offline

    def test_an_unpublished_search_is_a_sentence_not_a_key_error(self) -> None:
        registry = service_registry()
        saved = registry.snapshot()
        try:
            registry.restore({})
            with pytest.raises(RuntimeError, match=r"app\.composition"):
                archive_search()
        finally:
            registry.restore(saved)

    async def test_a_panel_without_a_search_says_so_and_stays_dead(self, state) -> None:
        """A half-wired application and a broken one look the same from here."""
        registry = service_registry()
        saved = registry.snapshot()
        try:
            registry.restore({})

            await _prepare(state)

            assert "app.composition" in state.error
            assert state.error_color == "red"
            assert state.ready is False
            assert state.can_search is False
        finally:
            registry.restore(saved)


class TestWithNoEmbedder:
    """§7.4's default, and the first thing a fresh installation shows."""

    async def test_the_panel_names_the_setting_before_anything_is_pressed(
        self, state, offline
    ) -> None:
        await _prepare(state)

        assert state.ready is True, "full text works without an embedder"
        assert state.semantic_ready is False
        assert state.semantic_note == NO_EMBEDDER
        assert "app_semantic_provider" in state.semantic_note
        assert state.error == ""

    async def test_choosing_the_semantic_path_shows_the_note_and_kills_the_button(
        self, state, offline
    ) -> None:
        await _prepare(state)
        state.set_query("rechnung")

        state.choose_path(SearchKind.SEMANTIC.value)

        assert state.semantic_blocked is True
        assert state.can_search is False
        assert state.semantic_note == NO_EMBEDDER

    async def test_firing_the_search_anyway_is_the_message_never_an_empty_list(
        self, state, offline, archive
    ) -> None:
        """A Reflex event is addressable by name, so the dead button is not
        the gate — and an empty result would read as "your archive holds
        nothing about this"."""
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.SEMANTIC)

        assert state.error == NO_EMBEDDER
        assert state.error_color == "yellow", "a missing setting is not a fault"
        assert state.hits == []
        assert state.summary == "", "no sentence claiming the archive is empty"
        assert archive.asked == [], "and the graph was never even opened"

    async def test_the_full_text_half_goes_on_working(self, state, offline) -> None:
        """The whole point of the default: an archive with no model configured
        is a complete archive missing one way of asking."""
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.FULLTEXT)

        assert state.error == ""
        assert [one.subject for one in state.hits] == ["Rechnung 4711", NO_SUBJECT]

    async def test_no_model_is_named_when_there_is_none(self, state, offline) -> None:
        await _prepare(state)

        assert state.embedding_model == ""


class TestFullText:
    async def test_a_hit_is_the_row_the_catalogue_answered_made_printable(
        self, state, offline
    ) -> None:
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.FULLTEXT)

        assert state.hits[0].model_dump() == {
            "message_id": "mail-1@example.com",
            "subject": "Rechnung 4711",
            "sender": "a@x.de",
            "when": f"{MARCH.astimezone():%d.%m.%y}",
            "score": 100.0,
            "score_label": "1.00",
        }

    async def test_the_relevance_is_ranked_within_this_one_answer(
        self, state, offline
    ) -> None:
        """RediSearch relevance is unbounded and means nothing across two
        queries, so the column is scaled against the best hit here."""
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.FULLTEXT)

        assert state.hits[1].score_label == "0.25"

    async def test_the_summary_says_how_many_and_in_what_order(
        self, state, offline
    ) -> None:
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.FULLTEXT)

        assert state.summary == "2 messages for “rechnung”, best match first."

    async def test_nothing_matching_is_a_sentence_and_not_an_error(
        self, state, offline, archive
    ) -> None:
        """The one empty result that is honest: the index answered, with
        nothing."""
        archive.rows(catalog.FULLTEXT_MESSAGES, FULLTEXT_COLUMNS, [])
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.FULLTEXT)

        assert state.hits == []
        assert state.error == ""
        assert state.summary == "Nothing in the archive matches “rechnung”."

    async def test_an_archive_that_was_never_migrated_says_that_instead(
        self, state, offline, archive
    ) -> None:
        """Measured on the vendored FalkorDB: a full-text query against a
        label with no index returns no rows rather than raising, so "nobody
        applied the migrations" and "nothing matched" are the same output one
        layer down."""
        archive.rows(catalog.FULLTEXT_MESSAGES, FULLTEXT_COLUMNS, [])
        _indexes(archive, fulltext=False)
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.FULLTEXT)

        assert state.error == NO_FULLTEXT_INDEX
        assert state.error_color == "yellow"
        assert state.summary == ""

    async def test_a_query_with_no_words_left_in_it_says_ask_differently(
        self, state, offline
    ) -> None:
        """Query operators are stripped before the store sees them, so this
        is a query that cannot be run rather than one that found nothing."""
        await _prepare(state)

        await _search_for(state, "*|-", SearchKind.FULLTEXT)

        assert "no searchable words" in state.error
        assert state.error_color == "yellow"
        assert state.hits == []

    async def test_a_graph_that_went_away_is_red_and_says_what_to_do(
        self, state, monkeypatch, caplog
    ) -> None:
        """Red rather than yellow, because it is a fault and not a setting —
        and a fixed sentence rather than the driver's, which can name a path
        inside this installation."""

        def dead() -> FakeGraph:
            raise ConnectionError("graph is down")

        search = SemanticSearch(cast(GraphSessionFactory, dead), SemanticConfig())
        published = _publish(search)
        next(published)
        try:
            await _prepare(state)

            with caplog.at_level(logging.ERROR, logger="mailarc_ui.insights.search"):
                await _search_for(state, "rechnung", SearchKind.FULLTEXT)

            assert state.error == SEARCH_FAILED
            assert state.error_color == "red", "a dead graph is a fault"
            assert "graph is down" in caplog.text, "the reason survives into the log"
        finally:
            next(published, None)


class TestSemantic:
    async def test_the_model_is_named_beside_the_selector(self, state, online) -> None:
        """A vector is only comparable with vectors from the same model, so
        the page says which one it is searching under."""
        await _prepare(state)

        assert state.semantic_ready is True
        assert state.embedding_model == "stub-embed"
        assert state.semantic_note == ""

    async def test_the_query_is_embedded_as_a_query_not_as_a_document(
        self, state, online, embedder
    ) -> None:
        """An instruction-tuned model embeds the two differently, which is
        why the port carries a purpose at all."""
        await _prepare(state)

        await _search_for(state, "unbezahlte rechnung", SearchKind.SEMANTIC)

        assert embedder.calls == [(("unbezahlte rechnung",), EmbedPurpose.QUERY)]

    async def test_a_distance_comes_back_as_a_similarity(self, state, online) -> None:
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.SEMANTIC)

        assert [one.score_label for one in state.hits] == ["0.90", "0.60"]
        assert state.summary == "2 messages for “rechnung”, closest first."

    async def test_a_half_embedded_archive_says_what_it_could_not_see(
        self, state, online, archive
    ) -> None:
        """A KNN over an archive four fifths of which has no vector answers
        short and looks exactly like a complete search over a small one."""
        archive.rows(catalog.VECTOR_COVERAGE, ["total", "embedded"], [[10, 2]])
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.SEMANTIC)

        assert "8 of 10 messages" in state.notice
        assert "stub-embed" in state.notice
        assert state.hits, "the hits are still shown; the notice sits beside them"

    async def test_a_fully_embedded_archive_says_nothing_at_all(
        self, state, online
    ) -> None:
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.SEMANTIC)

        assert state.notice == ""

    async def test_a_graph_with_no_vector_index_names_the_task_that_fixes_it(
        self, state, online, archive
    ) -> None:
        _indexes(archive, vector=0)
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.SEMANTIC)

        assert "task graph:upgrade" in state.error
        assert state.error_color == "yellow"
        assert state.hits == []

    async def test_an_index_of_the_wrong_dimension_is_refused_before_it_lies(
        self, state, online, archive
    ) -> None:
        """FalkorDB stores a wrong-length vector and silently declines to
        index it, so this is the failure that would otherwise report success
        and find nothing."""
        _indexes(archive, vector=DIMENSION + 1)
        await _prepare(state)

        await _search_for(state, "rechnung", SearchKind.SEMANTIC)

        assert "app_semantic_dimension" in state.error
        assert state.hits == []

    async def test_an_embedder_that_will_not_answer_is_a_fault_not_a_setting(
        self, state, archive
    ) -> None:
        broken = StubEmbedder(error=ConnectionError("connection refused"))
        published = _publish(
            SemanticSearch(_factory(archive), SemanticConfig(), embedder=broken)
        )
        next(published)
        try:
            await _prepare(state)

            await _search_for(state, "rechnung", SearchKind.SEMANTIC)

            assert state.error == SEARCH_FAILED
            assert state.error_color == "red"
        finally:
            next(published, None)

    async def test_a_drivers_own_words_never_reach_the_browser(
        self, state, archive, caplog
    ) -> None:
        """A FalkorDB or blob-store failure names a path inside this
        installation. The MCP server made the opposite choice for the same
        class of error and has a test asserting "mailstore" never crosses the
        wire; two surfaces over one archive should not disagree about that.
        The detail is not lost — it goes to the log, with its traceback."""
        leaky = StubEmbedder(
            error=RuntimeError("/Users/jens/.state/mailstore/ab/cd.eml is missing")
        )
        published = _publish(
            SemanticSearch(_factory(archive), SemanticConfig(), embedder=leaky)
        )
        next(published)
        try:
            await _prepare(state)

            with caplog.at_level(logging.ERROR, logger="mailarc_ui.insights.search"):
                await _search_for(state, "rechnung", SearchKind.SEMANTIC)

            assert state.error == SEARCH_FAILED
            assert "mailstore" not in state.error
            assert "mailstore" in caplog.text, "the detail belongs in the log"
        finally:
            next(published, None)


class TestTheBox:
    async def test_switching_path_throws_the_other_ones_answer_away(
        self, state, online
    ) -> None:
        """A relevance and a cosine similarity are different measurements on
        the same 0–1 column."""
        await _prepare(state)
        await _search_for(state, "rechnung", SearchKind.FULLTEXT)
        assert state.hits

        state.choose_path(SearchKind.SEMANTIC.value)

        assert state.hits == []
        assert state.summary == ""
        assert state.error == ""

    async def test_an_empty_box_is_told_to_type_something(
        self, state, offline, archive
    ) -> None:
        await _prepare(state)
        state.set_query("   ")

        await _run(state)

        assert state.error == NOTHING_ASKED
        assert state.error_color == "yellow"
        assert archive.asked == []

    async def test_the_button_is_dead_until_there_is_something_to_search_for(
        self, state, offline
    ) -> None:
        assert state.can_search is False, "and dead before prepare has run"
        await _prepare(state)
        assert state.can_search is False

        state.set_query("rechnung")

        assert state.can_search is True

    async def test_enter_does_what_the_button_does_and_nothing_else_does(
        self, state, offline
    ) -> None:
        await _prepare(state)
        state.set_query("rechnung")

        assert state.search_on_enter("a") is None
        assert "run" in str(state.search_on_enter("Enter"))

    async def test_enter_on_a_path_that_cannot_answer_does_nothing(
        self, state, offline
    ) -> None:
        await _prepare(state)
        state.set_query("rechnung")
        state.choose_path(SearchKind.SEMANTIC.value)

        assert state.search_on_enter("Enter") is None

    async def test_a_path_the_selector_never_offered_falls_back_to_full_text(
        self, state, offline
    ) -> None:
        """The kind arrives over the socket; an event's arguments are whatever
        the caller sent, and ``SearchKind("nonsense")`` would raise."""
        await _prepare(state)
        state.choose_path("nonsense")
        state.set_query("rechnung")

        await _run(state)

        assert state.error == ""
        assert state.hits, "answered by the path that always works"


class TestWhoIsAsking:
    """The page's ``admin_only`` is a render-time condition and gates nothing
    that goes over the socket. What this state would send back is subjects and
    sender addresses out of every mailbox in the installation."""

    async def test_a_non_admin_gets_a_sentence_and_asks_the_graph_nothing(
        self, state, offline, archive, monkeypatch
    ) -> None:
        signed_in_as(state, FakeUser(is_admin=False), monkeypatch)

        await _run(state)

        assert state.error == NOT_ADMIN
        assert state.hits == []
        assert archive.asked == []

    async def test_a_logged_out_visitor_gets_the_same(
        self, state, offline, archive, monkeypatch
    ) -> None:
        signed_in_as(state, None, monkeypatch)

        await _prepare(state)

        assert state.error == NOT_ADMIN
        assert state.ready is False
        assert archive.asked == []

    async def test_a_session_that_cannot_be_read_is_refused_not_trusted(
        self, state, offline, archive, monkeypatch
    ) -> None:
        async def unreachable(_self: object) -> object:
            raise LookupError("no EventContext")

        monkeypatch.setattr(type(state), "_current_user", unreachable)

        await _run(state)

        assert state.error == NOT_ADMIN
        assert archive.asked == []


class TestTheProjection:
    def test_a_message_with_no_subject_says_so_rather_than_showing_a_gap(
        self,
    ) -> None:
        from mailarc_analytics.semantic import SearchHit

        row = HitView.from_hit(SearchHit(message_id="x@y", score=0.5))

        assert row.subject == NO_SUBJECT
        assert row.when == ""
        assert row.score == 50.0

    def test_a_full_page_of_hits_does_not_claim_to_be_a_count(self) -> None:
        """The store was asked for *limit* rows and gave them; how many more
        there are is a number nobody counted."""
        full = found_summary(20, asked="rechnung", limit=20, kind=SearchKind.FULLTEXT)
        short = found_summary(1, asked="rechnung", limit=20, kind=SearchKind.FULLTEXT)

        assert "there may be more" in full
        assert short == "1 message for “rechnung”, best match first."

    def test_a_failure_carries_no_rows_and_no_summary(self) -> None:
        found = Found.failed("nope", color="yellow")

        assert found.hits == []
        assert found.summary == ""
        assert found.error_color == "yellow"

    def test_a_view_cannot_be_edited_once_read(self) -> None:
        row = HitView(message_id="x@y")

        with pytest.raises(ValidationError):
            row.subject = "changed"  # ty: ignore[invalid-assignment]


class TestTheComponents:
    """A prop appkit_mantine does not have only shows up when it is built."""

    @pytest.mark.parametrize("build", [search_card, hits_table])
    def test_it_builds_and_renders(self, build) -> None:
        assert isinstance(build(), rx.Component)
        assert build().render()

    def test_the_card_primes_itself_when_it_mounts(self) -> None:
        """Asserted off the event triggers, not the rendered tree: ``render()``
        puts props in and leaves handlers out, so a rendered string would go
        on passing with the wiring deleted. Nothing else calls ``prepare`` —
        the page's ``on_load`` primes the analytics readout and knows nothing
        about this panel."""
        triggers = search_card().event_triggers

        assert "on_mount" in triggers
        assert "prepare" in str(triggers["on_mount"])

    def test_the_panel_carries_the_search_card(self) -> None:
        assert "Find a message" in str(insights_panel().render())

    @pytest.mark.parametrize(
        "binding",
        [
            # The sentence naming the setting to change, and — separately —
            # the condition that decides whether it is shown at all: a
            # `rx.cond` renders as a node carrying both branches, so
            # asserting the var alone would survive the condition being
            # replaced by a constant.
            "semantic_note_rx_state_",
            "semantic_blocked_rx_state_', 'true_value'",
            # Red for a fault, yellow for a setting — a hard-coded colour
            # would tell a user with a healthy archive that something broke.
            "error_color_rx_state_",
            # How much of the archive a KNN could actually see.
            "notice_rx_state_?.valueOf?.()",
        ],
    )
    def test_the_binding_a_reader_depends_on_is_actually_rendered(
        self, binding
    ) -> None:
        assert binding in str(search_card().render())

    def test_a_hit_row_reads_its_fields_off_the_view(self) -> None:
        """Reflex resolves ``row.field`` inside a foreach over a pydantic
        model — the reason none of these projections is a dataclass."""
        rendered = str(hits_table().render())

        assert 'row_rx_state_?.["subject"]' in rendered
        assert 'row_rx_state_?.["score_label"]' in rendered
        assert 'row_rx_state_?.["message_id"]' in rendered
