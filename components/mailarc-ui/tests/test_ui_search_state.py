"""Tests for :mod:`mailarc_ui.search.state` and the model it projects onto.

The archive is a stand-in here, and deliberately so. What the reader does with
a :class:`~mailarc_core.archive.search.SearchFilters` is proven against a real
graph in ``components/mailarc-core/tests/archive/test_archive_search*.py``;
what this page can get wrong is everything on *this* side of that call — which
filters a filled form turns into, which of the two paths answers, what a row
prints, and what is left on screen when the archive does not answer at all.

So both services are subclasses that answer from a list and record what they
were asked. The one thing not faked is the seam: they are published into the
service registry, the way the composition root publishes them, and the state
finds them there.

Reading the message a row names is the mixin's, and is tested in
``test_ui_message_detail.py``. The one case here is the wiring — that a click
on a row reaches it with the right digest.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from appkit_commons.database.entities import Base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mailarc_analytics.semantic import (
    NO_EMBEDDER,
    SearchHit,
    SearchKind,
    SearchRequest,
    SearchResult,
    SemanticSearch,
    SemanticUnavailable,
    VectorCoverage,
)
from mailarc_core import ArchiveReader
from mailarc_core.archive.model import MessageLabel, MessageSummary
from mailarc_core.archive.search import MessageHit, SearchFilters, SearchPage
from mailarc_core.database.entities import MailAccountEntity
from mailarc_core.mail.model import LabelKind
from mailarc_ui.search import reads
from mailarc_ui.search.model import (
    ATTACH_ANY,
    ATTACH_WITH,
    ATTACH_WITHOUT,
    MODE_FULLTEXT,
    MODE_SEMANTIC,
    SEARCH_FAILED,
    ResultRow,
    filters_of,
    initials_of,
    parse_date,
    percent_label,
    relative_label,
)
from mailarc_ui.search.state import PAGE_SIZE, SEMANTIC_HITS, MailSearchState

READS_MODULE = "mailarc_ui.search.reads"

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

RAW = (
    b"From: Anna Bauer <anna@example.com>\r\n"
    b"To: jens@example.com\r\n"
    b"Date: Wed, 19 Aug 2026 14:28:00 +0000\r\n"
    b"Subject: Rechnung 2026\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Die Rechnung liegt bei.\r\n"
)

DIGEST = "a" * 64


def naive(text: str) -> datetime:
    """A wall-clock instant with no zone, written the way ISO writes it.

    What a picked date means to this archive: ``sent_at`` is stored as the
    header's own wall clock, and a bound converted to UTC first would make
    "from March 6th" mean the 5th for anybody east of Greenwich.
    """
    return datetime.fromisoformat(text)


def summary(number: int, **overrides: Any) -> MessageSummary:
    """One archived message, as the reader hands it over."""
    fields: dict[str, Any] = {
        "id": f"m{number}@example.com",
        "sender_name": "Anna Bauer",
        "sender_address": "anna@example.com",
        "subject": f"Rechnung {number}",
        "preview": "Die Rechnung liegt bei.",
        "sent_at": NOW - timedelta(hours=number),
        "has_attachments": number % 2 == 0,
        "eml_sha256": DIGEST,
    }
    return MessageSummary(**{**fields, **overrides})


class FakeReader(ArchiveReader):
    """The archive, answering from a list and remembering what it was asked.

    ``search_messages`` keeps the reader's own contract: a text search is
    ranked and un-counted, everything else comes with a total.
    """

    def __init__(self, summaries: list[MessageSummary]) -> None:
        self.summaries = summaries
        self.asked: list[tuple[SearchFilters, int, int]] = []
        self.hydrated: list[list[str]] = []
        self.relevance: dict[str, float] = {}
        self.error: Exception | None = None
        self.raw: dict[str, bytes] = {}

    def search_messages(
        self, filters: SearchFilters, *, limit: int = 50, offset: int = 0
    ) -> SearchPage:
        self.asked.append((filters, limit, offset))
        if self.error is not None:
            raise self.error
        page = self.summaries[offset : offset + limit]
        hits = tuple(
            MessageHit(summary=one, relevance=self.relevance.get(one.id))
            for one in page
        )
        counted = not filters.text.strip()
        return SearchPage(hits=hits, total=len(self.summaries) if counted else None)

    def messages_by_ids(self, ids: list[str]) -> list[MessageSummary]:
        self.hydrated.append(list(ids))
        known = {one.id: one for one in self.summaries}
        return [known[one] for one in ids if one in known]

    def raw_message(self, digest: str) -> bytes | None:
        return self.raw.get(digest)

    def remote_content_trusted(self, address: str) -> bool:
        return False


class FakeSearch(SemanticSearch):
    """The KNN, ranked the way the test says and never touching a graph."""

    def __init__(
        self,
        hits: tuple[SearchHit, ...] = (),
        *,
        available: bool = True,
        coverage: VectorCoverage | None = None,
        error: Exception | None = None,
    ) -> None:
        self.hits = hits
        self.offered = available
        self.coverage_ = coverage
        self.error = error
        self.asked: list[SearchRequest] = []

    @property
    def available(self) -> bool:
        return self.offered

    @property
    def model(self) -> str:
        return "test-embedder"

    async def semantic(self, request: SearchRequest) -> SearchResult:
        self.asked.append(request)
        if self.error is not None:
            raise self.error
        return SearchResult(
            kind=SearchKind.SEMANTIC, hits=self.hits, coverage=self.coverage_
        )


@pytest.fixture
def reader() -> FakeReader:
    return FakeReader([summary(one) for one in range(1, 5)])


@pytest.fixture
def search() -> FakeSearch:
    return FakeSearch()


@pytest.fixture
def published(reader: FakeReader, search: FakeSearch) -> Iterator[None]:
    """Both services where the composition root would leave them."""
    from appkit_commons.registry import service_registry

    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(ArchiveReader, reader)
    registry.register_as(SemanticSearch, search)
    yield
    registry.restore(saved)


@pytest.fixture
def offered(published: None) -> Iterator[list[dict[str, str]]]:
    """The account picker's options, without a database behind them."""
    options = [
        {"value": "1", "label": "Work"},
        {"value": "2", "label": "Private"},
    ]

    async def answer() -> list[dict[str, str]]:
        return list(options)

    with patch.object(reads, "account_options", answer):
        yield options


@pytest.fixture
def state(offered: list[dict[str, str]]) -> MailSearchState:
    """The page as it is driven."""
    return MailSearchState()


def _unlocked() -> Any:
    """Drive a background handler without Reflex's state lock under it."""
    return (
        patch.object(MailSearchState, "__aenter__", AsyncMock()),
        patch.object(MailSearchState, "__aexit__", AsyncMock(return_value=False)),
    )


async def _load(state: MailSearchState) -> None:
    """The page's ``on_load``, the way Reflex invokes a background task."""
    enter, leave = _unlocked()
    with enter, leave:
        await MailSearchState.load.fn(state)  # ty: ignore[unresolved-attribute]


async def _submit(state: MailSearchState) -> None:
    """The Search button, same reason as :func:`_load`."""
    enter, leave = _unlocked()
    with enter, leave:
        await MailSearchState.submit.fn(state)  # ty: ignore[unresolved-attribute]


async def _load_more(state: MailSearchState) -> None:
    """The Load more button, same reason as :func:`_load`."""
    enter, leave = _unlocked()
    with enter, leave:
        await MailSearchState.load_more.fn(state)  # ty: ignore[unresolved-attribute]


class TestTheFormAsFilters:
    """What eight strings from a browser mean to the archive."""

    def test_an_empty_form_asks_for_nothing(self) -> None:
        assert filters_of().empty is True

    def test_every_field_reaches_its_filter(self) -> None:
        filters = filters_of(
            query="  rechnung ",
            sender=" anna ",
            recipient="jens@",
            account_id=" 7 ",
        )

        assert filters.text == "rechnung"
        assert filters.sender == "anna"
        assert filters.recipient == "jens@"
        assert filters.account_id == "7"

    def test_both_date_shapes_are_read(self) -> None:
        """Mantine sends ISO; a person types what the field displays."""
        assert filters_of(date_from="2026-03-06").sent_from == naive("2026-03-06")
        assert filters_of(date_from="06.03.2026").sent_from == naive("2026-03-06")

    def test_a_date_that_is_not_one_filters_nothing(self) -> None:
        """A half-typed ``12.`` must not empty the result list."""
        assert filters_of(date_from="12.").sent_from is None
        assert filters_of(date_from="").sent_from is None

    def test_the_upper_bound_covers_the_whole_day_it_names(self) -> None:
        """A person who picks the 30th means through the 30th; the stored
        ``sent_at`` is compared as a string, so midnight would drop it."""
        until = filters_of(date_to="30.08.2026").sent_until

        assert until == naive("2026-08-30T23:59:59.999999")

    @pytest.mark.parametrize(
        ("segment", "expected"),
        [
            (ATTACH_ANY, None),
            (ATTACH_WITH, True),
            (ATTACH_WITHOUT, False),
            ("nonsense", None),
        ],
    )
    def test_attachments_is_a_tri_state(
        self, segment: str, expected: bool | None
    ) -> None:
        assert filters_of(attachments=segment).has_attachments is expected


class TestWhatARowPrints:
    """The formatting between a summary and a list row."""

    def test_initials_come_from_the_first_and_last_word(self) -> None:
        assert initials_of("Anna Bauer") == "A B"
        assert initials_of("Jens Uwe Rehpöhler") == "J R"

    def test_an_address_gives_up_two_letters_of_its_local_part(self) -> None:
        assert initials_of("shop@example.com") == "S H"
        assert initials_of("first.last@firma.de") == "F L"

    def test_a_sender_with_no_name_at_all_shows_nothing(self) -> None:
        assert initials_of("") == ""
        assert initials_of("@") == ""

    @pytest.mark.parametrize(
        ("ago", "expected"),
        [
            (timedelta(seconds=5), "now"),
            (timedelta(minutes=9), "9m"),
            (timedelta(hours=2), "2h"),
            (timedelta(days=3), "3d"),
            (timedelta(days=21), "3w"),
            (timedelta(days=800), "2y"),
        ],
    )
    def test_one_unit_rounded_down(self, ago: timedelta, expected: str) -> None:
        assert relative_label(NOW - ago, NOW) == expected

    def test_a_date_in_the_future_is_now_rather_than_a_negative(self) -> None:
        """``Date:`` is whatever a sender wrote, and *in 3d* is a claim this
        list has no business repeating."""
        assert relative_label(NOW + timedelta(days=3), NOW) == "now"

    def test_a_stored_instant_without_a_zone_is_read_as_utc(self) -> None:
        assert relative_label(naive("2026-08-30T10:00"), NOW) == "2h"

    def test_no_date_is_no_label(self) -> None:
        assert relative_label(None, NOW) == ""

    def test_a_relevance_is_a_percentage_and_nothing_is_nothing(self) -> None:
        assert percent_label(0.9234) == "92%"
        assert percent_label(0.0) == "0%"
        assert percent_label(None) == ""

    def test_a_row_is_what_the_reader_read_made_printable(self) -> None:
        row = ResultRow.from_summary(
            summary(
                2,
                labels=(MessageLabel(name="Kunden", kind=LabelKind.USER),),
            ),
            NOW,
            0.5,
        )

        assert row.sender == "Anna Bauer"
        assert row.initials == "A B"
        assert row.subject == "Rechnung 2"
        assert row.when_label == "2h"
        assert row.has_attachments is True
        assert row.eml_sha256 == DIGEST
        assert [chip.text for chip in row.labels] == ["Kunden"]
        assert row.relevance_label == "50%"

    def test_an_unranked_row_carries_no_score(self) -> None:
        assert ResultRow.from_summary(summary(1), NOW).relevance_label == ""

    def test_parse_date_reads_a_full_iso_instant_as_its_wall_clock(self) -> None:
        assert parse_date("2026-03-06T09:30:00+02:00") == naive("2026-03-06T09:30")


class TestOpeningThePage:
    """``load``: what can be offered, and the newest messages."""

    async def test_an_empty_form_lists_the_newest_messages(self, state) -> None:
        await _load(state)

        assert state.error == ""
        assert [row.id for row in state.rows] == [
            "m1@example.com",
            "m2@example.com",
            "m3@example.com",
            "m4@example.com",
        ]
        assert state.total == 4
        assert state.count_label == "4"
        assert state.searched is False
        assert state.searching is False

    async def test_the_listing_read_asks_for_nothing(self, state, reader) -> None:
        """The browse path *is* the empty filter — see ArchiveReader."""
        await _load(state)

        filters, limit, offset = reader.asked[-1]
        assert filters.empty is True
        assert (limit, offset) == (PAGE_SIZE, 0)

    async def test_it_offers_the_semantic_path_when_an_embedder_exists(
        self, state
    ) -> None:
        await _load(state)

        assert state.semantic_ready is True
        assert state.semantic_note == ""
        assert state.mode_options[1]["disabled"] is False

    async def test_it_says_why_the_semantic_path_is_off(self, state, search) -> None:
        """The sentence names the setting to change, and the segment is dead."""
        search.offered = False

        await _load(state)

        assert state.semantic_ready is False
        assert state.semantic_note == NO_EMBEDDER
        assert state.mode_options[1]["disabled"] is True

    async def test_it_fills_the_account_picker(self, state, offered) -> None:
        await _load(state)

        assert state.accounts == offered

    async def test_a_database_that_is_gone_costs_the_picker_and_not_the_list(
        self, state, caplog
    ) -> None:
        async def boom() -> list[dict[str, str]]:
            raise ConnectionError("no database")

        with (
            patch.object(reads, "account_options", boom),
            caplog.at_level(logging.ERROR),
        ):
            await _load(state)

        assert state.accounts == []
        assert state.error == ""
        assert len(state.rows) == 4


class TestSearching:
    """``submit``: what the form asks, and what comes back."""

    async def test_a_full_text_search_populates_the_rows(self, state, reader) -> None:
        reader.relevance = {"m1@example.com": 1.0, "m2@example.com": 0.5}
        await _load(state)
        state.set_query("rechnung")

        await _submit(state)

        assert state.searched is True
        assert state.searching is False
        assert [row.relevance_label for row in state.rows][:2] == ["100%", "50%"]
        assert reader.asked[-1][0].text == "rechnung"

    async def test_a_ranked_answer_is_shown_without_a_denominator(self, state) -> None:
        """Counting a full-text answer would mean running it twice."""
        await _load(state)
        state.set_query("rechnung")

        await _submit(state)

        assert state.total == 0
        assert state.count_label == "4"

    async def test_every_structured_field_reaches_the_archive(
        self, state, reader
    ) -> None:
        await _load(state)
        state.set_sender("anna")
        state.set_recipient("jens")
        state.set_date_from("01.08.2026")
        state.set_date_to("30.08.2026")
        state.choose_attachments(ATTACH_WITH)
        state.choose_account("2")

        await _submit(state)

        filters = reader.asked[-1][0]
        assert filters.sender == "anna"
        assert filters.recipient == "jens"
        assert filters.sent_from == naive("2026-08-01")
        assert filters.has_attachments is True
        assert filters.account_id == "2"

    async def test_an_account_nobody_offered_filters_nothing(self, state) -> None:
        """The value arrives over the socket, so it is checked, not trusted."""
        await _load(state)

        state.choose_account("99")

        assert state.account_id == ""

    async def test_a_typed_field_is_cut_before_it_reaches_a_query(self, state) -> None:
        state.set_query("x" * 5_000)

        assert len(state.query) < 5_000

    async def test_the_previous_answer_is_gone_before_the_new_one_arrives(
        self, state, reader
    ) -> None:
        await _load(state)
        reader.error = ConnectionError("graph is down")
        state.set_query("rechnung")

        await _submit(state)

        assert state.rows == []
        assert state.error == SEARCH_FAILED


class TestTheSemanticPath:
    """Question-only, hydrated by id, and ranked by the KNN."""

    async def test_it_hydrates_the_ranking_in_order(
        self, state, reader, search
    ) -> None:
        search.hits = (
            SearchHit(message_id="m3@example.com", score=0.9),
            SearchHit(message_id="m1@example.com", score=0.4),
        )
        await _load(state)
        state.choose_mode(MODE_SEMANTIC)
        state.set_query("offene rechnungen")

        await _submit(state)

        assert reader.hydrated[-1] == ["m3@example.com", "m1@example.com"]
        assert [row.id for row in state.rows] == ["m3@example.com", "m1@example.com"]
        assert [row.relevance_label for row in state.rows] == ["90%", "40%"]

    async def test_the_structured_fields_never_reach_the_search(
        self, state, reader, search
    ) -> None:
        """The KNN cannot honour them, so the form drops them rather than
        pretending they narrowed anything."""
        await _load(state)
        state.set_sender("anna")
        state.set_date_from("01.08.2026")
        state.choose_attachments(ATTACH_WITH)
        state.choose_mode(MODE_SEMANTIC)
        state.set_query("offene rechnungen")

        await _submit(state)

        assert search.asked[-1].text == "offene rechnungen"
        assert search.asked[-1].limit == SEMANTIC_HITS
        assert state._asked() == SearchFilters(text="offene rechnungen")  # noqa: SLF001
        assert reader.asked[-1][0].empty is True  # the browse read from `load`

    async def test_a_half_embedded_archive_says_so(self, state, search) -> None:
        search.hits = (SearchHit(message_id="m1@example.com", score=1.0),)
        search.coverage_ = VectorCoverage(model="test", total=10, embedded=4)
        await _load(state)
        state.choose_mode(MODE_SEMANTIC)
        state.set_query("rechnung")

        await _submit(state)

        assert state.notice != ""
        assert state.error == ""

    async def test_a_configuration_error_is_shown_as_written(
        self, state, search
    ) -> None:
        """A SemanticError names the setting to change; it is not a fault."""
        search.error = SemanticUnavailable("the vector index is missing")
        await _load(state)
        state.choose_mode(MODE_SEMANTIC)
        state.set_query("rechnung")

        await _submit(state)

        assert state.notice == "the vector index is missing"
        assert state.error == ""

    async def test_the_semantic_path_cannot_be_chosen_while_it_is_off(
        self, state, search
    ) -> None:
        search.offered = False
        await _load(state)

        state.choose_mode(MODE_SEMANTIC)

        assert state.mode == MODE_FULLTEXT

    async def test_a_question_with_no_words_in_it_is_a_notice(
        self, state, reader
    ) -> None:
        """The core sanitizer's refusal, which is about the ask and not the
        archive — answering with an empty list would read as an empty archive."""
        reader.error = ValueError("holds no searchable words")
        await _load(state)
        state.set_query("-@subject:*")

        await _submit(state)

        assert state.notice == "holds no searchable words"
        assert state.error == ""


class TestSwitchingPaths:
    async def test_choosing_a_path_drops_the_other_one_s_answer(self, state) -> None:
        """Two different measurements on the same 0–1 column."""
        await _load(state)
        state.set_query("rechnung")
        await _submit(state)

        state.choose_mode(MODE_SEMANTIC)

        assert state.rows == []
        assert state.total == 0
        assert state.searched is False
        assert state.selected_id == ""

    async def test_choosing_the_path_already_chosen_keeps_the_answer(
        self, state
    ) -> None:
        """Throwing the rows away is what switching costs, not what touching
        the control costs."""
        await _load(state)
        state.set_query("rechnung")
        await _submit(state)

        state.choose_mode(MODE_FULLTEXT)

        assert len(state.rows) == 4

    async def test_an_unknown_value_falls_back_to_the_path_that_always_works(
        self, state
    ) -> None:
        await _load(state)

        state.choose_mode("telepathy")

        assert state.mode == MODE_FULLTEXT

    async def test_reset_empties_the_form_and_asks_again(self, state) -> None:
        await _load(state)
        state.set_query("rechnung")
        state.set_sender("anna")
        state.choose_attachments(ATTACH_WITH)

        returned = state.reset_form()

        assert state.query == ""
        assert state.sender == ""
        assert state.attachments == ATTACH_ANY
        assert state.rows == []
        assert returned is MailSearchState.load


class TestPaging:
    async def test_a_full_page_is_reason_to_offer_another(self, state, reader) -> None:
        reader.summaries = [summary(one) for one in range(PAGE_SIZE + 5)]
        await _load(state)

        assert len(state.rows) == PAGE_SIZE
        assert state.offset == PAGE_SIZE
        assert state.has_more is True
        assert state.count_label == f"{PAGE_SIZE} of {PAGE_SIZE + 5}"

    async def test_load_more_appends_the_next_page(self, state, reader) -> None:
        reader.summaries = [summary(one) for one in range(PAGE_SIZE + 5)]
        await _load(state)

        await _load_more(state)

        assert reader.asked[-1][2] == PAGE_SIZE
        assert len(state.rows) == PAGE_SIZE + 5
        assert state.has_more is False

    async def test_load_more_pages_the_search_that_produced_the_page(
        self, state, reader
    ) -> None:
        """The form stays editable while an answer is up, so paging off it
        would append page two of a search nobody ran."""
        reader.summaries = [summary(one) for one in range(PAGE_SIZE + 5)]
        await _load(state)
        state.set_query("rechnung")
        await _submit(state)
        state.set_query("mueller")

        await _load_more(state)

        assert reader.asked[-1][0].text == "rechnung"

    async def test_load_more_does_nothing_when_everything_is_shown(
        self, state, reader
    ) -> None:
        await _load(state)
        asked = len(reader.asked)

        await _load_more(state)

        assert len(reader.asked) == asked

    async def test_a_ranked_page_offers_more_while_it_came_back_full(
        self, state, reader
    ) -> None:
        """A full-text answer has no total, so a full page is the only
        evidence another one exists."""
        reader.summaries = [summary(one) for one in range(PAGE_SIZE)]
        await _load(state)
        state.set_query("rechnung")

        await _submit(state)

        assert state.total == 0
        assert state.has_more is True

    async def test_the_semantic_answer_is_the_whole_answer(self, state, search) -> None:
        search.hits = (SearchHit(message_id="m1@example.com", score=1.0),)
        await _load(state)
        state.choose_mode(MODE_SEMANTIC)
        state.set_query("rechnung")
        await _submit(state)

        assert state.has_more is False


class TestOpeningAMessage:
    async def test_a_click_reads_the_original_the_row_names(
        self, state, reader
    ) -> None:
        reader.raw = {DIGEST: RAW}
        await _load(state)

        await state.select("m1@example.com")

        assert state.selected_id == "m1@example.com"
        assert state.view.subject == "Rechnung 2026"
        assert state.view.sender_address == "anna@example.com"

    async def test_an_id_from_nowhere_is_ignored(self, state) -> None:
        await _load(state)

        await state.select("m99@example.com")

        assert state.selected_id == ""

    async def test_a_search_that_loses_the_open_message_closes_it(
        self, state, reader
    ) -> None:
        reader.raw = {DIGEST: RAW}
        await _load(state)
        await state.select("m1@example.com")
        reader.summaries = [summary(9)]

        state.set_query("rechnung")
        await _submit(state)

        assert state.selected_id == ""
        assert state.view.subject == ""

    async def test_reopening_the_page_keeps_a_message_that_is_still_there(
        self, state, reader
    ) -> None:
        reader.raw = {DIGEST: RAW}
        await _load(state)
        await state.select("m1@example.com")

        await _load(state)

        assert state.selected_id == "m1@example.com"

    async def test_reopening_the_page_closes_a_message_that_is_gone(
        self, state, reader
    ) -> None:
        reader.raw = {DIGEST: RAW}
        await _load(state)
        await state.select("m1@example.com")
        reader.summaries = [summary(9)]

        await _load(state)

        assert state.selected_id == ""
        assert state.view.subject == ""


class TestWhenTheArchiveDoesNotAnswer:
    async def test_a_fault_is_a_sentence_and_never_the_driver_s_own(
        self, state, reader, caplog
    ) -> None:
        """A driver's message names a path inside this installation, and this
        string is rendered into a browser."""
        reader.error = ConnectionError("Error 61 connecting to 127.0.0.1:6379")

        with caplog.at_level(logging.ERROR):
            await _load(state)

        assert state.error == SEARCH_FAILED
        assert "6379" not in state.error
        assert state.searching is False

    async def test_a_failed_load_more_keeps_the_page_a_reader_is_looking_at(
        self, state, reader
    ) -> None:
        reader.summaries = [summary(one) for one in range(PAGE_SIZE + 5)]
        await _load(state)
        reader.error = ConnectionError("graph is down")

        await _load_more(state)

        assert len(state.rows) == PAGE_SIZE
        assert state.error == SEARCH_FAILED

    async def test_an_unregistered_search_leaves_the_page_working(self, state) -> None:
        from appkit_commons.registry import service_registry

        registry = service_registry()
        saved = registry.snapshot()
        registry.restore({k: v for k, v in saved.items() if k is not SemanticSearch})
        try:
            await _load(state)
        finally:
            registry.restore(saved)

        assert state.semantic_ready is False
        assert "composition" in state.semantic_note
        assert len(state.rows) == 4


class TestTheButton:
    async def test_it_is_dead_over_a_form_that_asks_for_nothing(self, state) -> None:
        await _load(state)

        assert state.can_search is False

    @pytest.mark.parametrize(
        "fill",
        [
            lambda one: one.set_query("rechnung"),
            lambda one: one.set_sender("anna"),
            lambda one: one.set_date_from("01.08.2026"),
            lambda one: one.choose_attachments(ATTACH_WITH),
        ],
    )
    async def test_any_filled_field_is_a_question(self, state, fill) -> None:
        await _load(state)

        fill(state)

        assert state.can_search is True

    async def test_the_semantic_path_needs_a_question(self, state) -> None:
        await _load(state)
        state.choose_mode(MODE_SEMANTIC)
        state.set_sender("anna")

        assert state.can_search is False

        state.set_query("rechnung")

        assert state.can_search is True

    async def test_enter_in_the_box_does_what_the_button_does(self, state) -> None:
        await _load(state)
        state.set_query("rechnung")

        assert "submit" in str(state.search_on_enter("Enter"))

    async def test_another_key_does_nothing(self, state) -> None:
        await _load(state)
        state.set_query("rechnung")

        assert state.search_on_enter("a") is None

    async def test_enter_cannot_run_a_search_a_click_could_not(self, state) -> None:
        await _load(state)

        assert state.search_on_enter("Enter") is None

    async def test_nothing_can_be_asked_while_something_is_being_asked(
        self, state
    ) -> None:
        state.set_query("rechnung")
        state.searching = True

        assert state.can_search is False


class TestTheEmptyLists:
    """Two empty lists that mean opposite things."""

    async def test_an_archive_that_holds_nothing_is_not_a_failed_search(
        self, state, reader
    ) -> None:
        reader.summaries = []

        await _load(state)

        assert state.nothing_matched is False
        assert state.count_label == "No messages"

    async def test_a_search_that_matched_nothing_says_so(self, state, reader) -> None:
        await _load(state)
        reader.summaries = []
        state.set_query("nichts")

        await _submit(state)

        assert state.nothing_matched is True


class TestTheAccountPicker:
    """``reads.account_options`` against a real database."""

    @pytest.fixture
    async def sessions(self, tmp_path) -> AsyncIterator[Any]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'accounts.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        def opened() -> Any:
            return _transaction(factory)

        async with factory() as session:
            session.add_all(
                [
                    MailAccountEntity(
                        provider="imap",
                        display_name="Work",
                        email_address="work@example.com",
                    ),
                    MailAccountEntity(
                        provider="gmail",
                        display_name="",
                        email_address="private@example.com",
                    ),
                ]
            )
            await session.commit()
        with patch(f"{READS_MODULE}.get_asyncdb_session", opened):
            yield factory
        await engine.dispose()

    async def test_it_offers_the_row_id_and_what_the_mailbox_is_called(
        self, sessions
    ) -> None:
        """The value is what the graph keys an ``Account`` node under, and an
        account whose name was left empty still has to be pickable."""
        options = await reads.account_options()

        assert options == [
            {"value": "2", "label": "private@example.com"},
            {"value": "1", "label": "Work"},
        ]


def _transaction(factory: async_sessionmaker[AsyncSession]) -> Any:
    """appkit's session contract: commit on the way out, roll back on a raise."""
    import contextlib

    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return opened()
