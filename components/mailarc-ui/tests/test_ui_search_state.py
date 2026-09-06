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
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from appkit_commons.database.entities import Base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mailarc_analytics import AnalyticsReader, GroupMembershipRow, TopicMembershipRow
from mailarc_analytics.nlq import (
    LlmConfig,
    NlqRefused,
    NlqUnavailable,
    QueryInterpreter,
    ResolvedQuery,
    Vocabulary,
)
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
from mailarc_core import ArchiveReader, TagStore
from mailarc_core.archive.model import (
    Conversation,
    MessageLabel,
    MessageSummary,
    Recipient,
    TagSummary,
)
from mailarc_core.archive.search import (
    AddressDirectory,
    AddressEntry,
    KnownAddresses,
    KnownAttachments,
    MessageHit,
    SearchFilters,
    SearchPage,
)
from mailarc_core.database.entities import MailAccountEntity
from mailarc_core.mail.model import LabelKind
from mailarc_ui.message_detail import FileKind
from mailarc_ui.search import memberships, reads
from mailarc_ui.search.answers import PAGE_SIZE, SEMANTIC_HITS
from mailarc_ui.search.model import (
    ATTACH_ANY,
    ATTACH_WITH,
    ATTACH_WITHOUT,
    MATCH_ALL,
    MATCH_ANY,
    MODE_FULLTEXT,
    MODE_NATURAL,
    MODE_SEMANTIC,
    NO_GROUP,
    READ_GROUPINGS,
    SEARCH_FAILED,
    UNFILED,
    Grouping,
    Membership,
    ResultRow,
    SearchAnswer,
    filters_of,
    initials_of,
    lines_of,
    parse_date,
    percent_label,
    relative_label,
    topic_label,
)
from mailarc_ui.search.state import MailSearchState

READS_MODULE = "mailarc_ui.search.reads"

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

ANNA = "anna@example.com"
CARL = "carl@firma.de"
BOB = "bob@example.com"
DORA = "dora@firma.de"
"""The addresses the fake archive holds — two senders, two recipients.

Kept apart on purpose: the pickers are role-split, so a test that used one
address in both fields could not tell the two lists apart.
"""

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
        self.threads: dict[str, Conversation] = {}
        self.grouped: list[list[str]] = []
        self.members: dict[str, list[MessageSummary]] = {}
        self.expanded: list[str] = []
        self.completed: list[list[str]] = []
        self.members_error: Exception | None = None
        """Fails the batched read alone. ``thread_error`` fails every
        conversation read there is, including the membership one the page
        cannot do without — and what has to be observable here is the page
        surviving a failure of the read that only *completes* it."""
        self.thread_error: Exception | None = None
        self.recipients: dict[str, Recipient] = {}
        self.addressed: list[list[str]] = []
        self.recipient_error: Exception | None = None
        self.known = KnownAddresses(senders=(ANNA, CARL), recipients=(BOB, DORA))
        self.address_error: Exception | None = None
        self.files = KnownAttachments(
            content_types=("application/pdf", "image/png", "application/zip"),
            untyped_names=("Rechnung.pdf", "notizen.md"),
        )
        self.file_error: Exception | None = None
        self.directory = AddressDirectory(
            entries=(
                AddressEntry(address=ANNA, names=("Anna Bauer",), domain="example.com"),
                AddressEntry(address=CARL, names=("Carl Winter",), domain="firma.de"),
                AddressEntry(address=BOB, domain="example.com"),
                AddressEntry(address=DORA, domain="firma.de"),
            )
        )
        """The names behind the addresses — what a question can actually say.

        Anna and Carl sign themselves; Bob and Dora never do. Both cases matter:
        a name is the only way a question reaches the first pair, and the second
        pair still has to be reachable by address."""
        self.directory_error: Exception | None = None

    def address_directory(self) -> AddressDirectory:
        if self.directory_error is not None:
            raise self.directory_error
        return self.directory

    def known_addresses(self) -> KnownAddresses:
        if self.address_error is not None:
            raise self.address_error
        return self.known

    def known_attachments(self) -> KnownAttachments:
        if self.file_error is not None:
            raise self.file_error
        return self.files

    def recipients_of(self, ids: list[str]) -> dict[str, Recipient]:
        self.addressed.append(list(ids))
        if self.recipient_error is not None:
            raise self.recipient_error
        return {one: self.recipients[one] for one in ids if one in self.recipients}

    def conversations_of(self, ids: list[str]) -> dict[str, Conversation]:
        self.grouped.append(list(ids))
        if self.thread_error is not None:
            raise self.thread_error
        return {one: self.threads[one] for one in ids if one in self.threads}

    def conversation_messages(
        self, conversation_id: str, *, limit: int = 200
    ) -> list[MessageSummary]:
        self.expanded.append(conversation_id)
        if self.thread_error is not None:
            raise self.thread_error
        return list(self.members.get(conversation_id, []))[:limit]

    def conversation_members(
        self, conversation_ids: list[str], *, limit: int = 200
    ) -> dict[str, list[MessageSummary]]:
        self.completed.append(list(conversation_ids))
        if self.members_error is not None:
            raise self.members_error
        if self.thread_error is not None:
            raise self.thread_error
        return {
            one: list(self.members[one])[:limit]
            for one in conversation_ids
            if one in self.members
        }

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


class FakeTagStore(TagStore):
    """The annotation layer, answering which of a page's rows wear a tag."""

    def __init__(self) -> None:
        self.tags: dict[str, tuple[TagSummary, ...]] = {}
        self.asked: list[list[str]] = []
        self.error: Exception | None = None

    def tags_of(self, ids: Sequence[str]) -> dict[str, tuple[TagSummary, ...]]:
        self.asked.append(list(ids))
        if self.error is not None:
            raise self.error
        return {one: self.tags[one] for one in ids if one in self.tags}


class FakeAnalytics(AnalyticsReader):
    """The derived layer, answering which topic and which group a row is in."""

    def __init__(self) -> None:
        self.topic_rows: dict[str, TopicMembershipRow] = {}
        self.group_rows: dict[str, GroupMembershipRow] = {}
        self.asked_topics: list[list[str]] = []
        self.asked_groups: list[list[str]] = []
        self.error: Exception | None = None

    def topics_of(self, ids: Sequence[str]) -> dict[str, TopicMembershipRow]:
        self.asked_topics.append(list(ids))
        if self.error is not None:
            raise self.error
        return {one: self.topic_rows[one] for one in ids if one in self.topic_rows}

    def groups_of(self, ids: Sequence[str]) -> dict[str, GroupMembershipRow]:
        self.asked_groups.append(list(ids))
        if self.error is not None:
            raise self.error
        return {one: self.group_rows[one] for one in ids if one in self.group_rows}


@pytest.fixture
def reader() -> FakeReader:
    return FakeReader([summary(one) for one in range(1, 5)])


class FakeInterpreter(QueryInterpreter):
    """One canned reading, or one raise, plus what it was shown.

    ``seen`` is the point of the last field: the promise this feature makes is
    that no archive data is sent to a model, and the vocabulary is the only
    argument that could carry any — so a test can assert on exactly what
    crossed.
    """

    def __init__(self) -> None:
        super().__init__(LlmConfig())
        self.answer = ResolvedQuery()
        self.error: Exception | None = None
        self.offer = True
        self.asked: list[str] = []
        self.seen: list[Vocabulary] = []

    @property
    def available(self) -> bool:
        return self.offer

    async def interpret(
        self, question: str, vocabulary: Vocabulary, *, today: Any = None
    ) -> ResolvedQuery:
        self.asked.append(question)
        self.seen.append(vocabulary)
        if self.error is not None:
            raise self.error
        return self.answer


@pytest.fixture
def interpreter() -> FakeInterpreter:
    return FakeInterpreter()


@pytest.fixture
def tags() -> FakeTagStore:
    return FakeTagStore()


@pytest.fixture
def analytics() -> FakeAnalytics:
    return FakeAnalytics()


@pytest.fixture
def search() -> FakeSearch:
    return FakeSearch()


@pytest.fixture
def published(
    reader: FakeReader,
    search: FakeSearch,
    tags: FakeTagStore,
    analytics: FakeAnalytics,
    interpreter: FakeInterpreter,
) -> Iterator[None]:
    """All five services where the composition root would leave them.

    The interpreter is registered even where a test does not use it, because
    that is what the composition root does — it publishes one whether or not a
    provider is configured, so that "no model set up" stays an ordinary state
    and never shows the half-wired-build sentence.
    """
    from appkit_commons.registry import service_registry

    registry = service_registry()
    saved = registry.snapshot()
    registry.register_as(ArchiveReader, reader)
    registry.register_as(SemanticSearch, search)
    registry.register_as(TagStore, tags)
    registry.register_as(AnalyticsReader, analytics)
    registry.register_as(QueryInterpreter, interpreter)
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
        await MailSearchState.load.fn(state)


async def _submit(state: MailSearchState) -> None:
    """The Search button, same reason as :func:`_load`."""
    enter, leave = _unlocked()
    with enter, leave:
        await MailSearchState.submit.fn(state)


async def _load_more(state: MailSearchState) -> None:
    """The Load more button, same reason as :func:`_load`."""
    enter, leave = _unlocked()
    with enter, leave:
        await MailSearchState.load_more.fn(state)


async def _switch(state: MailSearchState, value: str) -> None:
    """The Group by dropdown, same reason as :func:`_load`."""
    enter, leave = _unlocked()
    with enter, leave:
        await MailSearchState.choose_grouping.fn(state, value)


async def _expand(state: MailSearchState, conversation_id: str) -> None:
    """The heading's "show the whole conversation", same reason."""
    enter, leave = _unlocked()
    with enter, leave:
        await MailSearchState.show_whole_conversation.fn(state, conversation_id)


def row(number: int, **overrides: Any) -> ResultRow:
    """One result row, already printable."""
    return ResultRow.from_summary(summary(number, **overrides), NOW)


def filed(
    *numbers: int, key: str, total: int = 0, label: str = ""
) -> dict[str, Membership]:
    """These rows, filed under one group — what a membership read hands over."""
    return {
        f"m{one}@example.com": Membership(group_id=key, label=label, total=total)
        for one in numbers
    }


def drawn(
    rows: list[ResultRow],
    filed_as: dict[str, Membership] | None = None,
    *,
    grouping: Grouping = Grouping.CONVERSATION,
    collapsed: set[str] | None = None,
    whole: dict[str, list[ResultRow]] | None = None,
    busy: str = "",
) -> list:
    """:func:`lines_of` with every knob defaulted to the quiet position."""
    return lines_of(
        rows,
        filed_as or {},
        grouping=grouping,
        collapsed=collapsed or set(),
        whole=whole or {},
        busy=busy,
    )


class TestTheFormAsFilters:
    """What eight strings from a browser mean to the archive."""

    def test_an_empty_form_asks_for_nothing(self) -> None:
        assert filters_of().empty is True

    def test_every_field_reaches_its_filter(self) -> None:
        filters = filters_of(
            query="  rechnung ",
            senders=[ANNA, f" {CARL} "],
            recipients=[BOB, f" {DORA} "],
            account_id=" 7 ",
        )

        assert filters.text == "rechnung"
        assert filters.senders == (ANNA, CARL)
        assert filters.recipients == (BOB, DORA)
        assert filters.account_id == "7"

    def test_the_recipient_switch_is_the_only_or_on_the_form(self) -> None:
        """Every field is ANDed with every other one; the one place that is a
        choice is what several picked recipients mean."""
        assert filters_of(recipients=[BOB], recipient_match=MATCH_ALL).recipients_all
        assert not filters_of(
            recipients=[BOB], recipient_match=MATCH_ANY
        ).recipients_all

    def test_a_switch_position_nobody_offered_narrows(self) -> None:
        """The value crosses the socket, and the safe fallback is the narrow
        reading: a search somebody trusts to be narrow must not quietly
        widen."""
        assert filters_of(recipients=[BOB], recipient_match="either").recipients_all

    def test_an_empty_pick_is_not_an_address(self) -> None:
        assert filters_of(senders=["  "], recipients=["", "  "]).empty is True

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


class TestTheFileFilter:
    """The families the archive's two lists become, and back again."""

    async def test_the_picker_offers_a_family_per_file_the_archive_holds(
        self, state
    ) -> None:
        """Both halves of the catalogue in one menu: three stated types, and a
        ``notizen.md`` whose sender stated nothing — ``Rechnung.pdf`` adds no
        option because *PDF* is already there.

        Under the words a person reads — *Archive*, *Image*, *PDF*, *Text* —
        rather than commonest first, which is the order the archive hands its
        catalogue over in and the order the families are *chosen* in. See
        :mod:`mailarc_ui.search.ordering`."""
        await _load(state)

        assert [one["value"] for one in state.file_type_options] == [
            FileKind.ARCHIVE.value,
            FileKind.IMAGE.value,
            FileKind.PDF.value,
            FileKind.TEXT.value,
        ]

    async def test_a_picked_family_reaches_the_archive_as_content_types(
        self, state, reader
    ) -> None:
        """The whole join: a person picks *PDF* and the archive is asked
        something it can answer without knowing what a PDF is."""
        await _load(state)
        state.choose_file_kinds([FileKind.PDF.value])
        await _submit(state)

        filters, _limit, _offset = reader.asked[-1]
        assert filters.attachment_types == ("application/pdf",)

    async def test_a_picked_family_also_names_the_files_nobody_typed(
        self, state, reader
    ) -> None:
        """The other half of the same pick. ``Rechnung.pdf`` arrived as
        ``application/octet-stream``; without this the person who picked
        *PDF* would not be shown their own invoice."""
        await _load(state)
        state.choose_file_kinds([FileKind.PDF.value])
        await _submit(state)

        filters, _limit, _offset = reader.asked[-1]
        assert filters.attachment_names == ("Rechnung.pdf",)

    async def test_a_family_only_a_name_could_offer_reaches_the_archive(
        self, state, reader
    ) -> None:
        """*Text* is on the menu because of ``notizen.md`` alone, so the pick
        can only travel as a name — and it has to, or the option would answer
        with nothing."""
        await _load(state)
        state.choose_file_kinds([FileKind.TEXT.value])
        await _submit(state)

        filters, _limit, _offset = reader.asked[-1]
        assert filters.attachment_types == ()
        assert filters.attachment_names == ("notizen.md",)

    async def test_a_family_nobody_offered_is_dropped(self, state) -> None:
        """The value crosses the socket; a family this archive holds no files
        of would answer with an empty list that reads like an empty archive."""
        await _load(state)
        state.choose_file_kinds([FileKind.AUDIO.value, "nonsense"])

        assert state.file_kinds == []

    async def test_picking_a_family_is_reason_enough_to_search(self, state) -> None:
        """A file filter on its own is a question — the button must not stay
        dead because no words were typed."""
        await _load(state)
        assert state.can_search is False

        state.choose_file_kinds([FileKind.PDF.value])

        assert state.can_search is True

    async def test_an_unpicked_filter_reaches_the_archive_as_nothing(
        self, state, reader
    ) -> None:
        await _load(state)
        await _submit(state)

        filters, _limit, _offset = reader.asked[-1]
        assert filters.attachment_types == ()
        assert filters.attachment_names == ()

    async def test_reset_empties_it(self, state) -> None:
        await _load(state)
        state.choose_file_kinds([FileKind.PDF.value])

        state.reset_form()

        assert state.file_kinds == []

    async def test_a_graph_that_cannot_answer_costs_the_field_and_not_the_page(
        self, state, reader
    ) -> None:
        """One optional filter must not take the listing beside it down."""
        reader.file_error = ConnectionError("the graph is down")

        await _load(state)

        assert state.file_type_options == []
        assert state.error == ""
        assert len(state.rows) == 4

    async def test_an_archive_with_no_files_offers_no_picker(
        self, state, reader
    ) -> None:
        """What the form's ``rx.cond`` keys on: an empty list of options is
        how the field is left out rather than shown empty."""
        reader.files = KnownAttachments()

        await _load(state)

        assert state.file_type_options == []


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

    async def test_it_fills_the_two_address_pickers(self, state, reader) -> None:
        """Role-split: an address that only ever received mail is not a sender,
        and offering it under **From** would be a filter that cannot match."""
        await _load(state)

        assert state.sender_options == [ANNA, CARL]
        assert state.recipient_options == [BOB, DORA]

    async def test_the_pickers_read_alphabetically_whatever_the_archive_said(
        self, state, reader
    ) -> None:
        """The archive hands both lists over busiest first. An address book is
        looked up by name, and nobody knows how busy an address is before they
        have found it — see :mod:`mailarc_ui.search.ordering`."""
        reader.known = KnownAddresses(senders=(CARL, ANNA), recipients=(DORA, BOB))

        await _load(state)

        assert state.sender_options == [ANNA, CARL]
        assert state.recipient_options == [BOB, DORA]

    async def test_a_graph_that_is_gone_costs_the_pickers_and_not_the_list(
        self, state, reader
    ) -> None:
        """The address filters are optional fields; losing them must not cost
        the listing beside them."""
        reader.address_error = ConnectionError("graph is down")

        await _load(state)

        assert state.sender_options == []
        assert state.recipient_options == []
        assert state.rows != []
        assert state.error == ""

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
        state.choose_senders([ANNA, CARL])
        state.choose_recipients([BOB, DORA])
        state.set_date_from("01.08.2026")
        state.set_date_to("30.08.2026")
        state.choose_attachments(ATTACH_WITH)
        state.choose_account("2")

        await _submit(state)

        filters = reader.asked[-1][0]
        assert filters.senders == (ANNA, CARL)
        assert filters.recipients == (BOB, DORA)
        assert filters.recipients_all is True
        assert filters.sent_from == naive("2026-08-01")
        assert filters.has_attachments is True
        assert filters.account_id == "2"

    async def test_an_account_nobody_offered_filters_nothing(self, state) -> None:
        """The value arrives over the socket, so it is checked, not trusted."""
        await _load(state)

        state.choose_account("99")

        assert state.account_id == ""

    async def test_an_address_nobody_offered_filters_nothing(self, state) -> None:
        """Same guard, and a second reason for it here: an address this archive
        never sent from could only ever answer with an empty list, which reads
        like an empty archive rather than like a bad filter."""
        await _load(state)

        state.choose_senders([ANNA, "nobody@example.com"])
        state.choose_recipients([BOB, "nobody@example.com"])

        assert state.senders == [ANNA]
        assert state.recipients == [BOB]

    async def test_a_recipient_offered_under_from_is_still_not_a_sender(
        self, state
    ) -> None:
        """The two lists are separate lists, and the guard reads the right
        one — otherwise the split would be cosmetic."""
        await _load(state)

        state.choose_senders([BOB])
        state.choose_recipients([ANNA])

        assert state.senders == []
        assert state.recipients == []

    async def test_one_address_picked_twice_is_one_filter(self, state) -> None:
        """On the *all* reading a duplicate would be a second pattern matching
        the first one's node — harmless, and still not what was asked."""
        await _load(state)

        state.choose_senders([ANNA, CARL, ANNA])
        state.choose_recipients([BOB, DORA, BOB])

        assert state.senders == [ANNA, CARL]
        assert state.recipients == [BOB, DORA]

    async def test_the_recipient_switch_takes_only_its_two_positions(
        self, state
    ) -> None:
        await _load(state)

        state.choose_recipient_match(MATCH_ANY)
        assert state.recipient_match == MATCH_ANY

        state.choose_recipient_match("either")
        assert state.recipient_match == MATCH_ALL

    async def test_the_switch_reaches_the_archive(self, state, reader) -> None:
        await _load(state)
        state.choose_recipients([BOB, DORA])
        state.choose_recipient_match(MATCH_ANY)

        await _submit(state)

        assert reader.asked[-1][0].recipients_all is False

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
        state.choose_senders([ANNA])
        state.choose_recipients([BOB])
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
        state.choose_senders([ANNA])
        state.choose_recipients([BOB])
        state.choose_recipient_match(MATCH_ANY)
        state.choose_attachments(ATTACH_WITH)

        returned = state.reset_form()

        assert state.query == ""
        assert state.senders == []
        assert state.recipients == []
        assert state.recipient_match == MATCH_ALL
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
            lambda one: one.choose_senders([ANNA]),
            lambda one: one.choose_recipients([BOB]),
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
        state.choose_senders([ANNA])

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


def group(key: str, total: int) -> Conversation:
    return Conversation(id=key, total=total)


class TestGroupingTheAnswer:
    """:func:`lines_of` on its own — no state, no registry, no graph."""

    def test_grouping_off_draws_one_line_per_row(self) -> None:
        lines = drawn(
            [row(1), row(2)], filed(1, 2, key="c1", total=2), grouping=Grouping.NONE
        )

        assert [one.key for one in lines] == ["m:m1@example.com", "m:m2@example.com"]
        assert not any(one.is_header or one.is_section for one in lines)

    def test_a_conversation_becomes_a_heading_and_its_members(self) -> None:
        heading, member = drawn([row(1), row(2)], filed(1, 2, key="c1", total=2))

        assert heading.is_header is True
        assert heading.key == "c:c1"
        assert heading.subject == "Rechnung 1"
        assert heading.size_label == "2"
        assert member.key == "m:m2@example.com"
        assert member.indented is True

    def test_the_heading_states_how_much_of_the_conversation_is_shown(self) -> None:
        [heading] = drawn([row(1)], filed(1, key="c1", total=12))

        assert heading.size_label == "1 of 12"
        assert heading.can_expand is True

    def test_a_conversation_of_one_is_a_plain_row(self) -> None:
        """Chrome around a group of one says nothing."""
        [line] = drawn([row(1)], filed(1, key="c1", total=1))

        assert line.is_header is False
        assert line.can_expand is False

    def test_a_row_in_no_conversation_is_a_plain_row(self) -> None:
        [line] = drawn([row(1)])

        assert line.is_header is False
        assert line.key == "m:m1@example.com"

    def test_a_group_sits_where_its_first_seen_member_sat(self) -> None:
        """Grouping never moves a hit down the page; it pulls siblings up."""
        lines = drawn([row(1), row(2), row(3), row(4)], filed(2, 4, key="c1", total=2))

        assert [one.key for one in lines] == [
            "m:m1@example.com",
            "c:c1",
            "m:m4@example.com",
            "m:m3@example.com",
        ]

    def test_a_collapsed_group_draws_its_heading_and_nothing_else(self) -> None:
        lines = drawn(
            [row(1), row(2)], filed(1, 2, key="c1", total=2), collapsed={"c1"}
        )

        assert [one.key for one in lines] == ["c:c1"]
        assert lines[0].expanded is False

    def test_a_fetched_conversation_supplies_the_members(self) -> None:
        lines = drawn(
            [row(1)],
            filed(1, key="c1", total=3),
            whole={"c1": [row(1), row(2), row(3)]},
        )

        assert [one.key for one in lines] == [
            "c:c1",
            "m:m2@example.com",
            "m:m3@example.com",
        ]
        assert lines[0].can_expand is False
        assert lines[0].size_label == "3"

    def test_a_returned_hit_the_fetch_cut_off_is_kept(self) -> None:
        """The fetch is capped; dropping a hit would take it off the screen."""
        lines = drawn(
            [row(4)], filed(4, key="c1", total=9), whole={"c1": [row(1), row(2)]}
        )

        assert [one.key for one in lines] == [
            "c:c1",
            "m:m2@example.com",
            "m:m4@example.com",
        ]

    def test_only_the_group_being_fetched_says_it_is_busy(self) -> None:
        lines = drawn(
            [row(1), row(3)],
            {**filed(1, key="c1", total=4), **filed(3, key="c2", total=4)},
            busy="c1",
        )

        assert [one.busy for one in lines] == [True, False]

    def test_a_heading_carries_everything_its_row_printed(self) -> None:
        """A collapsed group must hide nothing the reader had already seen."""
        source = summary(2, subject="Angebot", preview="anbei")
        first = ResultRow.from_summary(source, NOW, 0.5)

        [heading] = drawn([first], filed(2, key="c1", total=4), collapsed={"c1"})

        assert heading.id == source.id
        assert heading.subject == "Angebot"
        assert heading.preview == "anbei"
        assert heading.initials == "A B"
        assert heading.has_attachments is True
        assert heading.relevance_label == "50%"


class TestSectioningTheAnswer:
    """The other six groupings: a labelled section over every group."""

    def test_senders_need_no_read_and_section_by_address(self) -> None:
        bob = row(2, sender_name="Bob Baker", sender_address="bob@example.com")

        lines = drawn([row(1), bob, row(3)], grouping=Grouping.SENDER)

        assert [one.key for one in lines] == [
            "g:anna@example.com",
            "m:m1@example.com",
            "m:m3@example.com",
            "g:bob@example.com",
            "m:m2@example.com",
        ]
        assert lines[0].is_section is True
        assert lines[0].label == "Anna Bauer"
        assert lines[0].size_label == "2"
        assert lines[3].label == "Bob Baker"

    def test_a_section_of_one_still_draws_its_header(self) -> None:
        """Unlike a conversation: a row does not say which sender it is under
        once the list is sectioned, so the section has to."""
        section, member = drawn([row(1)], grouping=Grouping.SENDER)

        assert section.is_section is True
        assert section.id == ""
        assert member.indented is True

    def test_subjects_section_on_the_normalised_subject(self) -> None:
        rows = [
            row(1, subject="Re: Angebot", subject_norm="angebot"),
            row(2, subject="Rechnung", subject_norm="rechnung"),
            row(3, subject="AW: Angebot", subject_norm="angebot"),
        ]

        lines = drawn(rows, grouping=Grouping.SUBJECT)

        assert [one.key for one in lines] == [
            "g:angebot",
            "m:m1@example.com",
            "m:m3@example.com",
            "g:rechnung",
            "m:m2@example.com",
        ]
        assert lines[0].label == "Re: Angebot"

    def test_a_row_without_a_subject_sits_in_the_bucket(self) -> None:
        [section, _member] = drawn(
            [row(1, subject="", subject_norm="")], grouping=Grouping.SUBJECT
        )

        assert section.group_id == NO_GROUP

    def test_topics_come_from_the_memberships(self) -> None:
        lines = drawn(
            [row(1), row(2), row(3)],
            filed(1, 3, key="topic:a", label="Angebot"),
            grouping=Grouping.TOPIC,
        )

        assert [one.key for one in lines] == [
            "g:topic:a",
            "m:m1@example.com",
            "m:m3@example.com",
            f"g:{NO_GROUP}",
            "m:m2@example.com",
        ]
        assert lines[0].label == "Angebot"
        assert lines[3].label == "No topic"

    @pytest.mark.parametrize(
        "grouping",
        [Grouping.TOPIC, Grouping.TAG, Grouping.RECURRING, Grouping.RECEIVER],
    )
    def test_a_row_the_read_did_not_file_lands_in_the_named_bucket(
        self, grouping: Grouping
    ) -> None:
        [section, member] = drawn([row(1)], grouping=grouping)

        assert section.group_id == NO_GROUP
        assert section.label == UNFILED[grouping]
        assert member.key == "m:m1@example.com"

    def test_a_closed_section_draws_its_header_and_nothing_else(self) -> None:
        lines = drawn(
            [row(1), row(2)], grouping=Grouping.SENDER, collapsed={"anna@example.com"}
        )

        assert [one.key for one in lines] == ["g:anna@example.com"]
        assert lines[0].expanded is False

    def test_the_bucket_can_be_closed_too(self) -> None:
        lines = drawn([row(1)], grouping=Grouping.TAG, collapsed={NO_GROUP})

        assert [one.key for one in lines] == [f"g:{NO_GROUP}"]


class TestNamingAGroup:
    """What a section says, per kind of group."""

    def test_a_topic_is_named_by_its_subject(self) -> None:
        membership = Membership.of_topic(
            TopicMembershipRow(topic_id="topic:a", label="Angebot", keywords=("x",))
        )

        assert membership == Membership(group_id="topic:a", label="Angebot")

    def test_a_topic_without_a_subject_is_named_by_its_words(self) -> None:
        found = topic_label(
            TopicMembershipRow(topic_id="topic:a", keywords=("a", "b", "c", "d"))
        )

        assert found == "a · b · c"

    def test_a_topic_with_neither_is_named_by_its_key(self) -> None:
        assert topic_label(TopicMembershipRow(topic_id="topic:8f3a2c")) == "8f3a2c"

    def test_a_recurring_group_is_named_by_its_size_and_key(self) -> None:
        membership = Membership.of_group(
            GroupMembershipRow(group_id="group:abcdef0", size=5, message_count=9)
        )

        assert membership.label == "5 people · abcdef0"

    def test_a_recipient_is_named_by_name_then_address(self) -> None:
        named = Membership.of_recipient(
            Recipient(address="bob@example.com", name="Bob")
        )
        bare = Membership.of_recipient(Recipient(address="bob@example.com"))

        assert (named.group_id, named.label) == ("bob@example.com", "Bob")
        assert bare.label == "bob@example.com"

    def test_the_first_tag_by_name_files_a_message(self) -> None:
        tags = (
            TagSummary(id="tag:a", name="Alpha"),
            TagSummary(id="tag:b", name="Beta"),
        )

        assert Membership.of_tags(tags) == Membership(group_id="tag:a", label="Alpha")
        assert Membership.of_tags(()) is None

    def test_every_grouping_that_needs_a_read_has_one(self) -> None:
        assert set(memberships._READERS) == set(READ_GROUPINGS)


class TestTheGroupingDropdown:
    async def test_the_list_is_grouped_by_conversation_before_anybody_asks(
        self, state
    ) -> None:
        assert state.grouping == Grouping.CONVERSATION

    async def test_loading_reads_the_conversations_of_its_page(
        self, state, reader
    ) -> None:
        reader.threads = {"m1@example.com": group("c1", 3)}

        await _load(state)

        assert reader.grouped == [[one.id for one in reader.summaries]]
        assert state._memberships["m1@example.com"].group_id == "c1"
        assert state.lines[0].is_header is True

    async def test_switching_to_none_reads_nothing_and_flattens(
        self, state, reader
    ) -> None:
        reader.threads = {one.id: group("c1", 4) for one in reader.summaries}
        await _load(state)
        before = len(reader.grouped)

        await _switch(state, Grouping.NONE)

        assert state.grouping == Grouping.NONE
        assert len(reader.grouped) == before
        assert not any(one.is_header or one.is_section for one in state.lines)

    async def test_switching_to_sender_reads_nothing_and_sections(
        self, state, reader, tags, analytics
    ) -> None:
        await _load(state)
        before = len(reader.grouped)

        await _switch(state, Grouping.SENDER)

        assert len(reader.grouped) == before
        assert (reader.addressed, tags.asked, analytics.asked_topics) == ([], [], [])
        assert state.lines[0].is_section is True
        assert state.lines[0].label == "Anna Bauer"

    async def test_switching_back_regroups_the_rows_on_screen(
        self, state, reader
    ) -> None:
        reader.threads = {one.id: group("c1", 4) for one in reader.summaries}
        await _load(state)
        await _switch(state, Grouping.NONE)

        await _switch(state, Grouping.CONVERSATION)

        assert state.grouping == Grouping.CONVERSATION
        assert state.lines[0].is_header is True

    async def test_switching_to_the_grouping_already_chosen_reads_nothing(
        self, state, reader
    ) -> None:
        await _load(state)
        before = len(reader.grouped)

        await _switch(state, Grouping.CONVERSATION)

        assert len(reader.grouped) == before

    async def test_a_grouping_nobody_offered_is_the_default(
        self, state, reader
    ) -> None:
        """The value arrives over the socket, so it is checked, not trusted."""
        await _load(state)
        await _switch(state, Grouping.NONE)

        await _switch(state, "everything")

        assert state.grouping == Grouping.CONVERSATION
        assert len(reader.grouped) == 2

    async def test_switching_forgets_the_closed_groups(self, state, reader) -> None:
        reader.threads = {one.id: group("c1", 4) for one in reader.summaries}
        await _load(state)
        MailSearchState.toggle_group.fn(state, "c1")

        await _switch(state, Grouping.SENDER)

        assert state._collapsed == set()

    async def test_switching_drops_the_old_memberships(self, state, reader) -> None:
        """A thread id is not a tag id: every row is in the bucket until the
        new read says otherwise."""
        reader.threads = {one.id: group("c1", 4) for one in reader.summaries}
        await _load(state)

        await _switch(state, Grouping.TAG)

        assert state._memberships == {}
        assert next(one.key for one in state.lines) == f"g:{NO_GROUP}"
        assert state.lines[0].label == "No tag"

    async def test_a_membership_read_that_fails_leaves_the_rows_in_one_bucket(
        self, state, reader, analytics
    ) -> None:
        """The rows are right; only the sections are missing."""
        await _load(state)
        analytics.error = RuntimeError("graph gone")

        await _switch(state, Grouping.TOPIC)

        assert len(state.rows) == len(reader.summaries)
        assert state.error == ""
        assert state.lines[0].label == "No topic"

    async def test_topics_are_read_for_the_rows_on_screen(
        self, state, reader, analytics
    ) -> None:
        analytics.topic_rows = {
            "m1@example.com": TopicMembershipRow(topic_id="topic:a", label="Angebot")
        }
        await _load(state)

        await _switch(state, Grouping.TOPIC)

        assert analytics.asked_topics == [[one.id for one in reader.summaries]]
        assert [one.key for one in state.lines] == [
            "g:topic:a",
            "m:m1@example.com",
            f"g:{NO_GROUP}",
            "m:m2@example.com",
            "m:m3@example.com",
            "m:m4@example.com",
        ]
        assert state.lines[0].label == "Angebot"

    async def test_tags_section_the_rows_that_wear_one(self, state, tags) -> None:
        tags.tags = {"m2@example.com": (TagSummary(id="tag:kunden", name="Kunden"),)}
        await _load(state)

        await _switch(state, Grouping.TAG)

        assert len(tags.asked) == 1
        assert [one.key for one in state.lines][:3] == [
            f"g:{NO_GROUP}",
            "m:m1@example.com",
            "m:m3@example.com",
        ]
        assert state.lines[4].key == "g:tag:kunden"
        assert state.lines[4].label == "Kunden"

    async def test_recurring_groups_come_from_the_derived_layer(
        self, state, analytics
    ) -> None:
        analytics.group_rows = {
            "m1@example.com": GroupMembershipRow(group_id="group:abcdef0", size=5)
        }
        await _load(state)

        await _switch(state, Grouping.RECURRING)

        assert len(analytics.asked_groups) == 1
        assert state.lines[0].label == "5 people · abcdef0"
        assert state.lines[2].label == "No group"

    async def test_receivers_come_from_the_archive(self, state, reader) -> None:
        reader.recipients = {
            "m1@example.com": Recipient(address="bob@example.com", name="Bob")
        }
        await _load(state)

        await _switch(state, Grouping.RECEIVER)

        assert reader.addressed == [[one.id for one in reader.summaries]]
        assert state.lines[0].label == "Bob"
        assert state.lines[2].label == "No recipient"

    async def test_a_failed_recipient_read_leaves_the_rows_alone(
        self, state, reader
    ) -> None:
        await _load(state)
        reader.recipient_error = RuntimeError("graph gone")

        await _switch(state, Grouping.RECEIVER)

        assert len(state.rows) == len(reader.summaries)
        assert state.error == ""

    async def test_a_load_reads_for_the_grouping_in_force(
        self, state, reader, tags
    ) -> None:
        state.grouping = Grouping.TAG.value

        await _load(state)

        assert reader.grouped == []
        assert tags.asked == [[one.id for one in reader.summaries]]

    async def test_a_page_read_under_another_grouping_is_not_filed(self, state) -> None:
        """A switch made while a page was in flight must not file that page's
        rows under the previous grouping's groups."""
        state.grouping = Grouping.SENDER.value

        state._apply(
            SearchAnswer(
                rows=(row(1),),
                memberships=filed(1, key="c1", total=3),
                grouping=Grouping.CONVERSATION.value,
            ),
            append=False,
        )

        assert state.rows == [row(1)]
        assert state._memberships == {}

    async def test_a_second_page_joins_the_section_its_first_page_made(
        self, state, reader
    ) -> None:
        reader.summaries = [summary(one) for one in range(1, PAGE_SIZE + 3)]
        await _load(state)
        await _switch(state, Grouping.SENDER)

        await _load_more(state)

        assert state.lines[0].key == "g:anna@example.com"
        assert state.lines[0].size_label == str(PAGE_SIZE + 2)

    async def test_closing_a_group_keeps_its_heading_up(self, state, reader) -> None:
        reader.threads = {one.id: group("c1", 4) for one in reader.summaries}
        await _load(state)

        MailSearchState.toggle_group.fn(state, "c1")

        assert [one.key for one in state.lines] == ["c:c1"]

    async def test_a_closed_group_opens_again(self, state, reader) -> None:
        reader.threads = {one.id: group("c1", 4) for one in reader.summaries}
        await _load(state)
        MailSearchState.toggle_group.fn(state, "c1")

        MailSearchState.toggle_group.fn(state, "c1")

        assert state._collapsed == set()
        assert len(state.lines) == len(reader.summaries)

    async def test_a_section_closes_and_opens_like_a_heading(self, state) -> None:
        await _load(state)
        await _switch(state, Grouping.SENDER)

        MailSearchState.toggle_group.fn(state, "anna@example.com")

        assert [one.key for one in state.lines] == ["g:anna@example.com"]

    async def test_the_bucket_nobody_read_can_be_closed(self, state) -> None:
        """It has a section and no membership, so the guard is the lines."""
        await _load(state)
        await _switch(state, Grouping.TOPIC)

        MailSearchState.toggle_group.fn(state, NO_GROUP)

        assert [one.key for one in state.lines] == [f"g:{NO_GROUP}"]

    async def test_a_group_nobody_drew_is_ignored(self, state, reader) -> None:
        """The value arrives over the socket, so it is checked, not trusted."""
        reader.threads = {one.id: group("c1", 4) for one in reader.summaries}
        await _load(state)

        MailSearchState.toggle_group.fn(state, "made-up")

        assert next(one.key for one in state.lines) == "c:c1"

    async def test_a_whole_conversation_is_only_fetched_under_conversations(
        self, state, reader
    ) -> None:
        reader.threads = {"m1@example.com": group("c1", 9)}
        reader.members = {"c1": reader.summaries}
        await _load(state)
        await _switch(state, Grouping.SENDER)

        await _expand(state, "c1")

        assert reader.expanded == []


class TestPagingAGroupedList:
    async def test_a_second_page_joins_the_group_its_heading_already_made(
        self, state, reader
    ) -> None:
        reader.summaries = [summary(one) for one in range(1, PAGE_SIZE + 3)]
        reader.threads = {
            f"m{one}@example.com": group("c1", 60) for one in (1, PAGE_SIZE + 1)
        }
        await _load(state)

        await _load_more(state)

        keys = [one.key for one in state.lines]
        assert keys[0] == "c:c1"
        assert f"m:m{PAGE_SIZE + 1}@example.com" in keys

    async def test_the_offset_still_counts_messages_and_not_lines(
        self, state, reader
    ) -> None:
        reader.summaries = [summary(one) for one in range(1, PAGE_SIZE + 3)]
        reader.threads = {f"m{one}@example.com": group("c1", 60) for one in (1, 2, 3)}

        await _load(state)

        assert state.offset == PAGE_SIZE

    async def test_a_new_search_forgets_every_group(self, state, reader) -> None:
        reader.threads = {one.id: group("c1", 4) for one in reader.summaries}
        await _load(state)
        MailSearchState.toggle_group.fn(state, "c1")
        reader.members = {"c1": reader.summaries}
        await _expand(state, "c1")
        state.query = "rechnung"

        await _submit(state)

        assert state._collapsed == set()
        assert state._whole == {}
        assert state._expanding == ""


class TestAConversationIsDrawnWhole:
    """A thread the answer only half matched is completed beside the page.

    The half of an exchange a person wrote themselves matches almost nothing
    they type — not the sender they filtered on, not the words they searched
    for — so a group built from the hits alone showed "2 of 3" and then refused
    to show the third. The missing members are read with the page and drawn
    under their heading; they are members and never hits, so nothing counts
    them.
    """

    async def test_the_missing_members_are_read_with_the_page(
        self, state, reader
    ) -> None:
        reader.summaries = [summary(1)]
        reader.threads = {"m1@example.com": group("c1", 3)}
        reader.members = {"c1": [summary(one) for one in (1, 2, 3)]}

        await _load(state)

        assert reader.completed == [["c1"]]
        assert [one.key for one in state.lines] == [
            "c:c1",
            "m:m2@example.com",
            "m:m3@example.com",
        ]

    async def test_the_chip_then_says_the_whole_size(self, state, reader) -> None:
        """``2 of 3`` was the symptom: a count naming a message not on screen."""
        reader.summaries = [summary(1)]
        reader.threads = {"m1@example.com": group("c1", 3)}
        reader.members = {"c1": [summary(one) for one in (1, 2, 3)]}

        await _load(state)

        assert state.lines[0].size_label == "3"
        assert state.lines[0].can_expand is False

    async def test_the_pulled_in_members_are_not_counted_as_hits(
        self, state, reader
    ) -> None:
        """The strip over the list says how many messages matched, and a
        member that was fetched matched nothing."""
        reader.summaries = [summary(1)]
        reader.threads = {"m1@example.com": group("c1", 3)}
        reader.members = {"c1": [summary(one) for one in (1, 2, 3)]}

        await _load(state)

        assert state.total == 1
        assert [one.id for one in state.rows] == ["m1@example.com"]

    async def test_a_group_that_is_already_whole_is_not_read_again(
        self, state, reader
    ) -> None:
        """One read per page, and none at all for the ordinary listing."""
        reader.summaries = [summary(1), summary(2)]
        reader.threads = {one.id: group("c1", 2) for one in reader.summaries}

        await _load(state)

        assert reader.completed == []

    async def test_nothing_is_read_while_the_list_is_grouped_otherwise(
        self, state, reader
    ) -> None:
        reader.summaries = [summary(1)]
        reader.threads = {"m1@example.com": group("c1", 3)}

        await _switch(state, Grouping.SENDER.value)

        assert reader.completed == []

    async def test_a_read_that_fails_leaves_the_hits_standing(
        self, state, reader
    ) -> None:
        """Completeness is worth a read, never the answer: a list of what
        matched is a worse page than a whole one and a far better one than a
        fault."""
        reader.summaries = [summary(1)]
        reader.threads = {"m1@example.com": group("c1", 3)}
        reader.members_error = RuntimeError("graph gone")

        await _load(state)

        assert state.error == ""
        assert [one.key for one in state.lines] == ["c:c1"]
        assert state.lines[0].size_label == "1 of 3"
        assert state.lines[0].can_expand is True

    async def test_switching_to_conversations_completes_them_too(
        self, state, reader
    ) -> None:
        """The dropdown reads the memberships of rows already on screen, and a
        group it draws from them has to be as whole as one a search brought."""
        reader.summaries = [summary(1)]
        reader.threads = {"m1@example.com": group("c1", 3)}
        reader.members = {"c1": [summary(one) for one in (1, 2, 3)]}
        await _switch(state, Grouping.NONE.value)
        await _load(state)

        await _switch(state, Grouping.CONVERSATION.value)

        assert reader.completed == [["c1"]]
        assert [one.key for one in state.lines] == [
            "c:c1",
            "m:m2@example.com",
            "m:m3@example.com",
        ]


class TestShowingTheWholeConversation:
    """The button that is left once the page completes its own groups.

    It still has a job: the automatic read is capped and it is allowed to
    fail, and either way the heading says so — ``can_expand`` is what puts the
    control back.
    """

    async def test_the_missing_members_are_fetched_and_drawn(
        self, state, reader
    ) -> None:
        reader.summaries = [summary(1)]
        reader.threads = {"m1@example.com": group("c1", 3)}
        reader.members_error = RuntimeError("graph gone")
        await _load(state)
        assert state.lines[0].can_expand is True
        reader.members_error = None
        reader.members = {"c1": [summary(one) for one in (1, 2, 3)]}

        await _expand(state, "c1")

        assert reader.expanded == ["c1"]
        assert [one.key for one in state.lines] == [
            "c:c1",
            "m:m2@example.com",
            "m:m3@example.com",
        ]
        assert state.lines[0].can_expand is False

    async def test_the_fetch_is_not_a_search(self, state, reader) -> None:
        """``searching`` puts the list-wide spinner up and takes Search away."""
        reader.threads = {"m1@example.com": group("c1", 9)}
        reader.members = {"c1": reader.summaries}
        await _load(state)

        await _expand(state, "c1")

        assert state.searching is False

    async def test_a_failed_fetch_leaves_the_group_as_it_was(
        self, state, reader
    ) -> None:
        reader.threads = {"m1@example.com": group("c1", 9)}
        await _load(state)
        before = [one.key for one in state.lines]
        reader.thread_error = RuntimeError("graph gone")

        await _expand(state, "c1")

        assert state.error == SEARCH_FAILED
        assert [one.key for one in state.lines] == before
        assert state._expanding == ""

    async def test_a_conversation_nobody_offered_is_never_fetched(
        self, state, reader
    ) -> None:
        await _load(state)

        await _expand(state, "made-up")

        assert reader.expanded == []

    async def test_a_fetched_member_can_be_opened(self, state, reader) -> None:
        reader.summaries = [summary(1)]
        reader.threads = {"m1@example.com": group("c1", 2)}
        reader.members = {"c1": [summary(1), summary(2)]}
        reader.raw = {DIGEST: RAW}
        await _load(state)
        await _expand(state, "c1")

        await MailSearchState.select.fn(state, "m2@example.com")

        assert state.selected_id == "m2@example.com"

    async def test_a_fetched_member_survives_the_next_page(self, state, reader) -> None:
        """``_apply`` checks the selection against both halves, or it closes it."""
        reader.summaries = [summary(one) for one in range(1, PAGE_SIZE + 3)]
        reader.threads = {"m1@example.com": group("c1", 60)}
        reader.members = {"c1": [summary(1), summary(500)]}
        reader.raw = {DIGEST: RAW}
        await _load(state)
        await _expand(state, "c1")
        await MailSearchState.select.fn(state, "m500@example.com")

        await _load_more(state)

        assert state.selected_id == "m500@example.com"


class TestGroupingASemanticAnswer:
    async def test_the_ranking_is_grouped_without_being_reordered(
        self, state, reader, search
    ) -> None:
        search.hits = tuple(
            SearchHit(message_id=f"m{one}@example.com", score=1.0 - one / 10)
            for one in (2, 1, 3)
        )
        reader.threads = {
            "m1@example.com": group("c1", 5),
            "m3@example.com": group("c1", 5),
        }
        await _load(state)
        state.mode = MODE_SEMANTIC
        state.query = "rechnung"

        await _submit(state)

        assert reader.hydrated[-1] == [
            "m2@example.com",
            "m1@example.com",
            "m3@example.com",
        ]
        assert [one.key for one in state.lines] == [
            "m:m2@example.com",
            "c:c1",
            "m:m3@example.com",
        ]
        assert state.lines[1].subject == "Rechnung 1"


class TestTheNaturalSegment:
    """Whether it can be offered, and what happens when it cannot."""

    async def test_it_is_dead_until_an_interpreter_exists(
        self, state, interpreter
    ) -> None:
        interpreter.offer = False
        await _load(state)

        assert state.mode_options[2]["value"] == MODE_NATURAL
        assert state.mode_options[2]["disabled"] is True

    async def test_it_is_live_once_one_does(self, state, interpreter) -> None:
        await _load(state)

        assert state.mode_options[2]["disabled"] is False
        assert state.llm_note == ""

    async def test_it_is_appended_and_never_inserted(self, state) -> None:
        """A selector whose options move under a reader is a control they stop
        trusting — and full text stays first because it is the one that always
        works."""
        await _load(state)

        assert [one["value"] for one in state.mode_options] == [
            MODE_FULLTEXT,
            MODE_SEMANTIC,
            MODE_NATURAL,
        ]

    async def test_the_note_names_a_remedy_a_reader_can_act_on(
        self, state, interpreter
    ) -> None:
        """On the desktop bundle there is no shell into the environment, so a
        sentence whose only remedy was a variable would be one its reader could
        not act on — which is why it names the page first."""
        interpreter.offer = False
        await _load(state)

        assert "no interpreter model is configured" in state.llm_note
        assert "/admin/embedder" in state.llm_note

    async def test_a_half_wired_build_says_so_instead(self, state) -> None:
        """The developer's sentence, and it must never be shown for an
        installation that simply has no model set up — which is why the
        composition root publishes an interpreter either way.

        The lookup is patched rather than the registry emptied: what is under
        test is the page's answer when the service is absent, and going through
        the registry would make the test depend on how that spells "nothing
        registered" instead.
        """

        def missing() -> QueryInterpreter:
            raise RuntimeError(reads.NO_INTERPRETER)

        with patch.object(reads, "query_interpreter", missing):
            await _load(state)

        assert "app.composition" in state.llm_note
        assert state.llm_ready is False

    async def test_it_cannot_be_chosen_while_it_is_off(
        self, state, interpreter
    ) -> None:
        interpreter.offer = False
        await _load(state)

        state.choose_mode(MODE_NATURAL)

        assert state.mode == MODE_FULLTEXT

    async def test_choosing_it_is_allowed_once_it_is_on(self, state) -> None:
        await _load(state)

        state.choose_mode(MODE_NATURAL)

        assert state.mode == MODE_NATURAL
        assert state.natural_chosen is True

    async def test_it_does_not_grey_the_fields_out(self, state) -> None:
        """The opposite of semantic, and the whole design: the reading lands in
        the fields, so they have to stay editable to be correctable."""
        await _load(state)

        state.choose_mode(MODE_NATURAL)

        assert state.semantic_chosen is False

    async def test_switching_away_forgets_what_was_understood(self, state) -> None:
        await _load(state)
        state.choose_mode(MODE_NATURAL)
        state.understood = "something about the last question"

        state.choose_mode(MODE_FULLTEXT)

        assert state.understood == ""

    async def test_a_page_load_demotes_a_mode_whose_service_went_away(
        self, state, interpreter
    ) -> None:
        """Left standing it would be a Search button that can never light up,
        with nothing on screen saying why."""
        await _load(state)
        state.choose_mode(MODE_NATURAL)
        interpreter.offer = False

        await _load(state)

        assert state.mode == MODE_FULLTEXT

    async def test_search_needs_a_sentence_and_an_interpreter(
        self, state, interpreter
    ) -> None:
        await _load(state)
        state.choose_mode(MODE_NATURAL)

        assert state.can_search is False
        state.query = "Rechnungen von Anna"
        assert state.can_search is True
        interpreter.offer = False
        state.llm_ready = False
        assert state.can_search is False

    async def test_a_picked_sender_alone_is_not_a_question(self, state) -> None:
        """A structured filter is what this mode *produces*, so a form with no
        sentence in it has nothing to interpret."""
        await _load(state)
        state.choose_mode(MODE_NATURAL)
        state.choose_senders([ANNA])

        assert state.can_search is False


class TestReadingAQuestionIntoTheForm:
    """The step in front of the search, and the one that has to be visible."""

    async def test_the_reading_fills_the_fields_it_resolved(
        self, state, interpreter, reader
    ) -> None:
        interpreter.answer = ResolvedQuery(
            text="Rechnung",
            senders=(ANNA,),
            after="2026-01-01",
            has_attachments=True,
        )
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "Rechnungen von Anna Bauer seit Januar"

        await _submit(state)

        assert state.senders == [ANNA]
        assert state.query == "Rechnung"
        assert state.date_from == "2026-01-01"
        assert state.attachments == ATTACH_WITH

    async def test_what_it_filled_in_is_what_was_searched(
        self, state, interpreter, reader
    ) -> None:
        """One filter builder and one search: the form is written first, then
        ``_asked`` reads it exactly as it would after typing."""
        interpreter.answer = ResolvedQuery(text="Rechnung", senders=(ANNA,))
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "Rechnungen von Anna"

        await _submit(state)

        asked, _, _ = reader.asked[-1]
        assert asked.senders == (ANNA,)
        assert asked.text == "Rechnung"

    async def test_an_address_the_page_never_offered_is_dropped(
        self, state, interpreter
    ) -> None:
        """The guard that matters most. A model invents plausible addresses,
        and one that is not in the picker's options would not render as a pill
        — so the reader would see an empty **From** over a search that really
        had been narrowed."""
        interpreter.answer = ResolvedQuery(senders=("invented@nowhere.example",))
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "von irgendwem"

        await _submit(state)

        assert state.senders == []

    async def test_a_family_this_archive_has_none_of_is_dropped(
        self, state, interpreter
    ) -> None:
        interpreter.answer = ResolvedQuery(families=("hologram",))
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "mit Hologrammen"

        await _submit(state)

        assert state.file_kinds == []

    async def test_a_mailbox_nobody_offered_is_dropped(
        self, state, interpreter
    ) -> None:
        interpreter.answer = ResolvedQuery(account_id="99")
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "in meinem anderen Postfach"

        await _submit(state)

        assert state.account_id == ""

    async def test_the_whole_form_is_written_and_not_only_part_of_it(
        self, state, interpreter
    ) -> None:
        """A leftover date under a freshly interpreted question would be a
        filter nobody asked for and nobody could account for."""
        await _load(state)
        state.mode = MODE_NATURAL
        state.date_from = "2020-01-01"
        state.attachments = ATTACH_WITHOUT
        state.recipient_match = MATCH_ANY
        state.query = "irgendwas"
        interpreter.answer = ResolvedQuery(text="irgendwas")

        await _submit(state)

        assert state.date_from == ""
        assert state.attachments == ATTACH_ANY
        assert state.recipient_match == MATCH_ALL

    async def test_the_model_is_shown_the_archives_own_vocabulary(
        self, state, interpreter
    ) -> None:
        """Grounding: the interpreter matches names against exactly the options
        the form can display, so a reading it produces is always one the form
        can show."""
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "von Anna"

        await _submit(state)

        shown = interpreter.seen[-1]
        assert [one.address for one in shown.senders] == [ANNA, CARL]
        assert shown.senders[0].names == ("Anna Bauer",)
        assert {one.value for one in shown.accounts} == {"1", "2"}

    async def test_a_lost_directory_costs_the_grounding_and_not_the_page(
        self, state, interpreter, reader
    ) -> None:
        """The bargain every optional read on this page strikes."""
        reader.directory_error = RuntimeError("graph is down")
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "von Anna"

        await _submit(state)

        assert interpreter.seen[-1].senders == ()
        assert state.error == ""


class TestWhatTheReadingCouldNotDo:
    """``understood``, and why it is not the answer's notice."""

    async def test_an_unresolved_name_is_said_rather_than_dropped(
        self, state, interpreter
    ) -> None:
        interpreter.answer = ResolvedQuery(
            text="Vertrag Frau Schmidt", unresolved=("Frau Schmidt",)
        )
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "Vertrag von Frau Schmidt"

        await _submit(state)

        assert "Frau Schmidt" in state.understood
        assert state.query == "Vertrag Frau Schmidt"

    async def test_a_note_from_the_reading_is_carried_through(
        self, state, interpreter
    ) -> None:
        interpreter.answer = ResolvedQuery(
            senders=(ANNA,), notes=("Sorted newest first, so 'Aufladung' was dropped.",)
        )
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "wann war die letzte Aufladung von Anna"

        await _submit(state)

        assert "Aufladung" in state.understood

    async def test_a_clean_reading_says_nothing(self, state, interpreter) -> None:
        """The filled-in fields already show what was searched, so a sentence
        restating them is noise a reader learns to skip — and then misses the
        one time it matters."""
        interpreter.answer = ResolvedQuery(text="Rechnung", senders=(ANNA,))
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "Rechnungen von Anna"

        await _submit(state)

        assert state.understood == ""

    async def test_it_survives_paging(self, state, interpreter, reader) -> None:
        """The reason it is its own var. ``notice`` belongs to an answer and
        ``_apply`` overwrites it on every read, **Load more** included."""
        interpreter.answer = ResolvedQuery(text="Vertrag", unresolved=("Frau Schmidt",))
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "Vertrag von Frau Schmidt"
        await _submit(state)
        said = state.understood
        state.offset = PAGE_SIZE

        await _load_more(state)

        assert state.understood == said
        assert said != ""


class TestWhenTheQuestionCannotBeRead:
    """No empty filter may stand in for a failure — it is the whole archive."""

    async def test_a_missing_model_is_a_notice_and_not_a_fault(
        self, state, interpreter, reader
    ) -> None:
        interpreter.error = NlqUnavailable("Natural-language search: no model.")
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "von Anna"
        before = len(reader.asked)

        await _submit(state)

        assert "no model" in state.notice
        assert state.error == ""
        assert len(reader.asked) == before

    async def test_a_model_that_would_not_answer_is_a_notice_too(
        self, state, interpreter, reader
    ) -> None:
        interpreter.error = NlqRefused("the model did not answer with a filter")
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "von Anna"
        before = len(reader.asked)

        await _submit(state)

        assert "did not answer with a filter" in state.notice
        assert len(reader.asked) == before

    async def test_nothing_is_searched_and_the_list_stays_empty(
        self, state, interpreter
    ) -> None:
        """The failure this catches: an empty ``SearchFilters`` is not "nothing
        matched", it is the browse-the-whole-archive read — so a swallowed
        failure would answer a narrow question with every message there is."""
        interpreter.error = NlqUnavailable("nope")
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "von Anna"

        await _submit(state)

        assert state.rows == []
        assert state.searching is False

    async def test_an_unexpected_failure_is_a_fault(self, state, interpreter) -> None:
        """Never a notice: a notice says what a person can do about it, and
        this one has no such sentence to offer."""
        interpreter.error = ZeroDivisionError("something went very wrong")
        await _load(state)
        state.mode = MODE_NATURAL
        state.query = "von Anna"

        await _submit(state)

        assert state.error == SEARCH_FAILED
        assert state.notice == ""
