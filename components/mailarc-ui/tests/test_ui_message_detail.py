"""Tests for :mod:`mailarc_ui.message_detail`.

The point of this package is that a page can read a mail without the reading
pane knowing anything about the list beside it, so every test here drives a
state the application does not have: :class:`OtherDetailState`, which lists the
mixin and brings nothing else. What it proves is that the vars, the computed
vars and the handlers materialise on *it* — not only on the one state that
happens to list the mixin today, which would also be true of a plain base
class — and that two such states hold two separate open messages.

The reading itself runs against the real :class:`ArchiveReader` and a real blob
store, with only the graph session faked, the way ``mailarc-core``'s own reader
tests drive it. The formatting functions need neither and are checked directly.
"""

import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import cast

import appkit_mantine as mn
import pytest
import reflex as rx
from appkit_commons.registry import service_registry

from mailarc_analytics import AnalyticsReader, GroupMembershipRow, TopicMembershipRow
from mailarc_core import ArchiveReader
from mailarc_core.archive import TagStore
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import Address, BlobKind, TagSummary
from mailarc_core.archive.reader import GraphSessionFactory
from mailarc_core.mail.model import EmailAddress, LabelKind
from mailarc_ui.message_detail import (
    CHIP_ICONS,
    CHIP_KINDS,
    DRAWABLE_CHIPS,
    FRAME_CSP,
    GRAPH_GLYPH,
    TAB_GRAPH,
    TAB_MESSAGE,
    TAB_SOURCE,
    ChipKind,
    MessageDetailState,
    address_label,
    address_suffix,
    chips_for,
    chips_of,
    decode_raw,
    frame_document,
    group_chip,
    label_text,
    long_date_label,
    message_body,
    message_header,
    message_tabs,
    message_view,
    raw_message_view,
    remote_content_bar,
    sender_label,
    size_label,
    tag_chip,
    topic_chip,
)

RAW = (
    b"From: Jens <jens@example.com>\r\n"
    b"To: Anna Bauer <anna@example.com>\r\n"
    b"Date: Wed, 19 Aug 2026 14:28:00 +0000\r\n"
    b"Subject: SwiftScan\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Erstellt mit SwiftScan.\r\n"
)

HTML_RAW = (
    b"From: shop@example.com\r\n"
    b"To: jens@example.com\r\n"
    b"Subject: Rechnung\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>Lieber <b>Gast</b></p>"
    b'<img src="https://tracker.example/pixel.gif">\r\n'
)


class OtherDetailState(MessageDetailState, rx.State):
    """A page the application does not have — the whole argument for the mixin.

    Empty on purpose: whatever it can do, it got from the mixin.
    """


class FakeAnalytics:
    """The two membership reads the header makes, scripted per message."""

    def __init__(self) -> None:
        self.topics: dict[str, TopicMembershipRow] = {}
        self.groups: dict[str, GroupMembershipRow] = {}
        self.failing = False

    def topics_of(self, ids: Sequence[str]) -> dict[str, TopicMembershipRow]:
        if self.failing:
            raise ConnectionError("graph is down")
        return {one: self.topics[one] for one in ids if one in self.topics}

    def groups_of(self, ids: Sequence[str]) -> dict[str, GroupMembershipRow]:
        if self.failing:
            raise ConnectionError("graph is down")
        return {one: self.groups[one] for one in ids if one in self.groups}


class FakeTags:
    """The one read the header makes of the annotation layer."""

    def __init__(self) -> None:
        self.tags: dict[str, tuple[TagSummary, ...]] = {}
        self.failing = False

    def tags_of(self, ids: Sequence[str]) -> dict[str, tuple[TagSummary, ...]]:
        if self.failing:
            raise ConnectionError("the tag store is down")
        return {one: self.tags.get(one, ()) for one in ids}


class TrustSession:
    """A ``runic.ogm.Session`` stand-in with nothing in it but addresses."""

    def __init__(self) -> None:
        self.addresses: dict[str, Address] = {}

    def get(self, cls: type, pk: str) -> Address | None:
        assert cls is Address
        return self.addresses.get(pk)

    def __enter__(self) -> TrustSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def blobs(tmp_path) -> BlobStore:
    return BlobStore(ArchiveConfig(store_dir=tmp_path / "blobs"))


@pytest.fixture
def digest(blobs: BlobStore) -> str:
    return blobs.put(RAW, BlobKind.MESSAGE)


@pytest.fixture
def html_digest(blobs: BlobStore) -> str:
    return blobs.put(HTML_RAW, BlobKind.MESSAGE)


@pytest.fixture
def session() -> TrustSession:
    return TrustSession()


@pytest.fixture
def analytics() -> FakeAnalytics:
    return FakeAnalytics()


@pytest.fixture
def tags() -> FakeTags:
    return FakeTags()


@pytest.fixture
def published(
    session: TrustSession,
    blobs: BlobStore,
    analytics: FakeAnalytics,
    tags: FakeTags,
) -> Iterator[ArchiveReader]:
    """Everything the pane reads through, left where composition would leave it."""
    registry = service_registry()
    saved = registry.snapshot()
    reader = ArchiveReader(cast(GraphSessionFactory, lambda: session), blobs)
    registry.register_as(ArchiveReader, reader)
    registry.register_as(AnalyticsReader, cast(AnalyticsReader, analytics))
    registry.register_as(TagStore, cast(TagStore, tags))
    yield reader
    registry.restore(saved)


@pytest.fixture
def state(published: ArchiveReader) -> OtherDetailState:
    """A state that has the pane and no list at all."""
    return OtherDetailState()


class TestWhatTheMixinBrings:
    def test_a_state_that_only_lists_it_gets_the_whole_pane(self) -> None:
        """Vars, computed vars and handlers are copied into the concrete class;
        without that, a component built on this state would resolve nothing."""
        assert "selected_id" in OtherDetailState.base_vars
        assert "view" in OtherDetailState.base_vars
        assert {"has_selection", "frame_html", "remote_blocked"} <= set(
            OtherDetailState.computed_vars
        )
        assert {"show_tab", "allow_remote_once", "allow_remote_for_sender"} <= set(
            OtherDetailState.event_handlers
        )

    def test_it_is_a_mixin_and_not_a_page_state(self) -> None:
        """It has no substate of its own; instantiating it is the mistake the
        mixin flag exists to catch."""
        with pytest.raises(Exception, match="mixin"):
            MessageDetailState()

    async def test_two_pages_hold_two_open_messages(self, state, digest) -> None:
        """The reason for a mixin rather than one shared substate: every state
        that lists it keeps a selection of its own."""
        from mailarc_ui.search import MailSearchState

        other = MailSearchState()
        await state._open_message("m1@example.com", digest)

        assert state.selected_id == "m1@example.com"
        assert other.selected_id == ""
        assert other.view.subject == ""


class TestOpeningAMessage:
    async def test_it_reads_the_original_by_its_digest(self, state, digest) -> None:
        await state._open_message("m1@example.com", digest)

        assert state.has_selection is True
        assert state.raw == RAW.decode()
        assert state.raw_truncated is False
        assert state.loading_raw is False
        assert state.view.subject == "SwiftScan"
        assert state.view.sender_name == "Jens"
        assert state.view.sender_address == "jens@example.com"
        assert [one.label for one in state.view.recipients] == [
            "Anna Bauer <anna@example.com>"
        ]
        assert [one.address for one in state.view.recipients] == ["anna@example.com"]
        assert state.view.body_text == "Erstellt mit SwiftScan.\n"

    async def test_the_name_and_the_address_reach_the_header_apart(
        self, state, digest
    ) -> None:
        """A mail client prints the name and then the address after it in
        grey, so the header needs the two halves rather than one label."""
        await state._open_message("m1@example.com", digest)

        assert state.sender_address_line == "<jens@example.com>"

    async def test_a_sender_who_gave_no_name_is_printed_once(
        self, state, html_digest
    ) -> None:
        """`shop@example.com <shop@example.com>` is what the naive version of
        that same line prints."""
        await state._open_message("m4@example.com", html_digest)

        assert state.view.sender_name == "shop@example.com"
        assert state.sender_address_line == ""

    async def test_a_message_without_a_stored_original_says_so(self, state) -> None:
        await state._open_message("m2@example.com", "")

        assert state.selected_id == "m2@example.com"
        assert "No original" in state.raw
        assert "No original" in state.view.body_text

    async def test_a_missing_blob_says_so(self, state) -> None:
        await state._open_message("m3@example.com", "0" * 64)

        assert "missing" in state.raw
        assert "missing" in state.view.body_text

    async def test_a_new_selection_forgets_the_previous_view(
        self, state, html_digest, digest
    ) -> None:
        await state._open_message("m4@example.com", html_digest)

        await state._open_message("m2@example.com", "")

        assert state.view.body_html == ""
        assert state.remote_allowed is False


class TestRemoteContent:
    async def test_it_starts_blocked(self, state, html_digest) -> None:
        await state._open_message("m4@example.com", html_digest)

        assert state.view.remote_references == 1
        assert state.remote_blocked is True
        assert "1 remote reference" in state.remote_notice
        assert "img-src data:;" in state.frame_html

    async def test_allow_once_opens_this_render_only(self, state, html_digest) -> None:
        await state._open_message("m4@example.com", html_digest)

        state.allow_remote_once()

        assert state.remote_blocked is False
        assert "img-src data: https: http:" in state.frame_html
        # Nothing was recorded: opening the same mail again asks again.
        await state._open_message("m4@example.com", html_digest)
        assert state.remote_blocked is True

    async def test_allow_for_sender_is_recorded_on_the_address(
        self, state, session, html_digest
    ) -> None:
        session.addresses["shop@example.com"] = Address(id="shop@example.com")
        await state._open_message("m4@example.com", html_digest)

        await state.allow_remote_for_sender()

        assert session.addresses["shop@example.com"].remote_trusted is True
        assert state.message_note == ""
        # The next mail from that sender is open from the start.
        await state._open_message("m4@example.com", html_digest)
        assert state.remote_allowed is True

    async def test_a_sender_the_archive_does_not_know_still_allows_once(
        self, state, html_digest
    ) -> None:
        await state._open_message("m4@example.com", html_digest)

        await state.allow_remote_for_sender()

        assert state.remote_allowed is True
        assert "allowed once" in state.message_note


class TestTheTabs:
    def test_the_readable_view_is_up_first(self, state) -> None:
        assert state.tab == TAB_MESSAGE

    def test_switching_to_the_source_and_back(self, state) -> None:
        state.show_tab(TAB_SOURCE)
        assert state.tab == TAB_SOURCE

        state.show_tab(TAB_MESSAGE)
        assert state.tab == TAB_MESSAGE

    def test_a_value_that_is_not_a_tab_falls_back_to_the_message(self, state) -> None:
        state.show_tab("whatever")

        assert state.tab == TAB_MESSAGE

    async def test_the_tab_outlives_the_selection(self, state, digest) -> None:
        state.show_tab(TAB_SOURCE)

        await state._open_message("m1@example.com", digest)

        assert state.tab == TAB_SOURCE


class TestTheFrameDocument:
    def test_the_policy_comes_before_the_mail(self) -> None:
        document = frame_document("<p>hi</p>")

        assert document.index("Content-Security-Policy") < document.index("<p>hi</p>")
        assert "default-src 'none'" in document
        assert "img-src data:" in document

    def test_the_agreed_policy_lets_pictures_in_and_scripts_stay_out(self) -> None:
        document = frame_document("<p>hi</p>", allow_remote=True)

        assert "img-src data: https: http:" in document
        assert "default-src 'none'" in document


class TestDecodingTheRaw:
    """UTF-8, then what the part said, then Windows-1252 with replacement.

    The middle step and the order it sits in are new, and they are the fix for
    a real screenshot: a contact card written by a German mail program read
    ``Gesch�ftsleitung`` in the panel, because every attachment used to be
    decoded as UTF-8 with replacement and nothing else was tried.
    """

    def test_utf8_comes_through_whole(self) -> None:
        assert decode_raw("Grüße".encode()) == ("Grüße", False)

    def test_a_single_byte_encoding_is_read_rather_than_replaced(self) -> None:
        """``Gr��e`` is what this used to return."""
        assert decode_raw("Grüße".encode("cp1252")) == ("Grüße", False)

    def test_what_the_part_said_is_believed_when_utf8_fails(self) -> None:
        text, _ = decode_raw("Ελλάδα".encode("iso-8859-7"), charset="iso-8859-7")

        assert text == "Ελλάδα"

    def test_utf8_wins_over_a_label_that_disagrees_with_it(self) -> None:
        """Latin-1 accepts every byte, so a UTF-8 file labelled ``iso-8859-1``
        would decode without error into ``GrÃ¼ÃŸe`` and nothing would
        notice. Trusting the label first is how that gets archived."""
        text, _ = decode_raw("Grüße".encode(), charset="iso-8859-1")

        assert text == "Grüße"

    def test_a_charset_no_codec_has_falls_through_rather_than_raising(self) -> None:
        text, _ = decode_raw("Grüße".encode(), charset="x-mac-erfunden")

        assert text == "Grüße"

    def test_a_byte_that_is_in_no_encoding_at_all_is_still_not_fatal(self) -> None:
        """0x81 is unassigned in Windows-1252 too, so this is the floor: a
        replacement character, and never an exception."""
        text, truncated = decode_raw(b"Gr\x81\xdfe")

        assert text == "Gr�ße"
        assert truncated is False

    def test_a_long_original_is_cut_after_decoding(self) -> None:
        text, truncated = decode_raw(("é" * 10).encode(), limit=4)

        assert text == "éééé"
        assert truncated is True


class TestTheLabelText:
    @pytest.mark.parametrize(
        ("name", "text"),
        [
            ("INBOX", "Inbox"),
            ("CATEGORY_PROMOTIONS", "Promotions"),
            ("CATEGORY_SOCIAL", "Social"),
            ("UNREAD", "Unread"),
            ("SENT", "Sent"),
        ],
    )
    def test_a_system_name_reads_like_a_mail_client_prints_it(
        self, name: str, text: str
    ) -> None:
        assert label_text(name, LabelKind.SYSTEM) == text

    def test_a_users_name_is_left_exactly_as_they_wrote_it(self) -> None:
        assert label_text("Kunden/Bauer GmbH", LabelKind.USER) == "Kunden/Bauer GmbH"
        assert label_text("INBOX", LabelKind.FOLDER) == "INBOX"


class TestTheHeaderLabels:
    def test_an_address_with_a_name_shows_both(self) -> None:
        address = EmailAddress(address="anna@example.com", display_name="Anna")

        assert address_label(address) == "Anna <anna@example.com>"

    def test_the_bold_half_is_the_name_or_the_address_behind_it(self) -> None:
        """And nothing at all for a mail that arrived without a `From`."""
        named = EmailAddress(address="anna@example.com", display_name="Anna")

        assert sender_label(named) == "Anna"
        assert sender_label(EmailAddress(address="anna@example.com")) == (
            "anna@example.com"
        )
        assert sender_label(None) == ""

    def test_the_grey_chunk_after_a_name_is_the_address_in_brackets(self) -> None:
        assert address_suffix("Anna", "anna@example.com") == "<anna@example.com>"

    def test_there_is_no_grey_chunk_when_the_name_is_the_address(self) -> None:
        assert address_suffix("anna@example.com", "anna@example.com") == ""
        assert address_suffix("", "") == ""

    def test_an_address_without_a_name_is_just_the_address(self) -> None:
        assert (
            address_label(EmailAddress(address="anna@example.com"))
            == "anna@example.com"
        )
        assert address_label(None) == ""

    def test_the_long_date_is_local_and_has_a_weekday(self) -> None:
        sent = datetime(2026, 8, 19, 14, 28, tzinfo=UTC)

        assert long_date_label(sent) == sent.astimezone().strftime("%a, %d.%m.%Y %H:%M")
        assert long_date_label(None) == ""

    def test_a_date_this_zone_cannot_print_costs_one_cell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same forgeable header that took the insights page down.

        ``Date: Fri, 31 Dec 9999 23:59:59 +0000`` parses, ``_sent_at``
        range-checks nothing, and ``astimezone()`` then raises ``OverflowError``
        in every zone east of UTC — which is the developer's own, and a routine
        spam trick besides. In a header that has to be one empty cell, not a
        message that will not render.
        """
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        far_future = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

        assert long_date_label(far_future) == ""

    def test_the_other_end_of_the_range_costs_one_cell_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Year 0001 overflows west of UTC, so the guard is two-sided."""
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        long_ago = datetime(1, 1, 1, tzinfo=UTC)

        assert long_date_label(long_ago) == ""

    @pytest.mark.parametrize(
        ("size", "label"),
        [(0, "0 B"), (512, "512 B"), (12 * 1024, "12 KB"), (1400 * 1024, "1.4 MB")],
    )
    def test_sizes_read_like_a_file_list(self, size: int, label: str) -> None:
        assert size_label(size) == label


class TestTheChipsOnTheHeader:
    """What the open message belongs to, named the way a chip can hold it.

    The projection only — no state, no graph — so the three label rules are
    checkable on their own. That the *reads* behind them fail one at a time is
    ``TestOpeningAMessage``'s business.
    """

    def test_a_topic_is_named_by_its_subject_first(self) -> None:
        row = TopicMembershipRow(
            topic_id="topic:8f3a", label="Rechnungen", keywords=("rechnung", "pdf")
        )

        assert topic_chip(row).label == "Rechnungen"

    def test_a_topic_with_no_common_subject_falls_back_to_its_words(self) -> None:
        """Three of them and not all: a chip is a name, not a sentence."""
        row = TopicMembershipRow(
            topic_id="topic:8f3a",
            keywords=("rechnung", "pdf", "august", "mahnung"),
        )

        assert topic_chip(row).label == "rechnung · pdf · august"

    def test_a_topic_with_neither_says_so_in_a_word(self) -> None:
        """Not the digest a listing prints — a header shows the one topic this
        message is in, and eight hex characters name it no better."""
        assert topic_chip(TopicMembershipRow(topic_id="topic:8f3a")).label == "Topic"

    def test_a_tag_wears_the_name_a_person_gave_it(self) -> None:
        assert tag_chip(TagSummary(id="t1", name="Steuer")).label == "Steuer"
        assert tag_chip(TagSummary(id="t1")).label == "t1"

    @pytest.mark.parametrize(
        ("size", "label"),
        [(5, "5 people"), (1, "1 person"), (0, "Group")],
    )
    def test_a_group_has_a_size_rather_than_a_name(self, size, label) -> None:
        row = GroupMembershipRow(group_id="g1", size=size)

        assert group_chip(row).label == label

    def test_the_order_is_about_then_filed_then_addressed(self) -> None:
        chips = chips_for(
            TopicMembershipRow(topic_id="topic:1", label="Nord 42"),
            [TagSummary(id="t1", name="Steuer"), TagSummary(id="t2", name="2026")],
            GroupMembershipRow(group_id="g1", size=3),
        )

        assert [one.label for one in chips] == ["Nord 42", "Steuer", "2026", "3 people"]

    def test_what_the_archive_does_not_know_is_simply_not_a_chip(self) -> None:
        """An archive nobody has rebuilt has no topics and no groups, and the
        header shows the tags it does have rather than three placeholders."""
        chips = chips_for(None, [TagSummary(id="t1", name="Steuer")], None)

        assert [one.kind for one in chips] == [ChipKind.TAG.value]
        assert chips_for(None, [], None) == []

    def test_a_topic_and_a_tag_can_be_drawn_and_a_group_cannot(self) -> None:
        """The flag the header renders the pill from. A recurring group is a
        node in the graph but no view is rooted at one."""
        chips = chips_for(
            TopicMembershipRow(topic_id="topic:1"),
            [TagSummary(id="t1")],
            GroupMembershipRow(group_id="g1", size=2),
        )

        assert [one.drawable for one in chips] == [True, True, False]

    def test_every_kind_of_chip_has_a_glyph(self) -> None:
        """The map and the chip kinds, checked against each other: a kind
        added to one and not the other is a `KeyError` at the moment a header
        draws. The other two kinds are not chips — a message and an address
        are already named on the header and wear a glyph instead."""
        assert set(CHIP_ICONS) == set(CHIP_KINDS)
        assert set(CHIP_KINDS) < set(ChipKind)
        assert DRAWABLE_CHIPS <= set(ChipKind)

    def test_only_the_three_kinds_that_are_chips_become_chips(self) -> None:
        """`chips_for` answers about belonging, and a message does not belong
        to itself."""
        chips = chips_for(
            TopicMembershipRow(topic_id="topic:1", label="Nord 42"),
            [TagSummary(id="t1", name="Steuer")],
            GroupMembershipRow(group_id="g1", size=3),
        )

        assert {one.kind for one in chips} <= {one.value for one in CHIP_KINDS}


class TestTheChipTab:
    """Pressing a chip opens a third tab that stands for it."""

    async def test_the_header_learns_what_the_message_belongs_to(
        self, state, digest, analytics, tags
    ) -> None:
        analytics.topics["m1@example.com"] = TopicMembershipRow(
            topic_id="topic:1", label="Nord 42"
        )
        tags.tags["m1@example.com"] = (TagSummary(id="t1", name="Steuer"),)

        await state._open_message("m1@example.com", digest)

        assert [one.label for one in state.chips] == ["Nord 42", "Steuer"]

    async def test_a_read_that_fails_costs_its_own_chips_and_no_more(
        self, state, digest, analytics, tags
    ) -> None:
        """A header is not a page: a graph that went away must not take the
        mail beside it down."""
        analytics.failing = True
        tags.tags["m1@example.com"] = (TagSummary(id="t1", name="Steuer"),)

        await state._open_message("m1@example.com", digest)

        assert [one.label for one in state.chips] == ["Steuer"]
        assert state.view.subject == "SwiftScan"

    async def test_the_annotation_layer_fails_on_its_own_too(
        self, state, digest, analytics, tags
    ) -> None:
        """The other side of the same rule: three reads, three failures, and
        the two that answered still reach the header."""
        tags.failing = True
        analytics.topics["m1@example.com"] = TopicMembershipRow(
            topic_id="topic:1", label="Nord 42"
        )

        await state._open_message("m1@example.com", digest)

        assert [one.label for one in state.chips] == ["Nord 42"]

    def test_no_message_is_no_read_at_all(self, published) -> None:
        """Nothing selected asks the archive nothing."""
        assert chips_of("") == []

    async def test_pressing_one_opens_the_tab_on_it(self, state) -> None:
        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:1", "Nord 42")

        assert state.chip_open is True
        assert state.chip_kind == ChipKind.TOPIC.value
        assert state.chip_id == "topic:1"
        assert state.chip_label == "Nord 42"
        assert state.tab == TAB_GRAPH

    async def test_a_chip_with_nothing_behind_it_opens_nothing(self, state) -> None:
        await state.show_chip_graph(ChipKind.TOPIC.value, "", "Nord 42")

        assert state.chip_open is False
        assert state.tab == TAB_MESSAGE

    async def test_the_graph_tab_is_only_selectable_while_it_is_there(
        self, state
    ) -> None:
        """A stored `graph` would otherwise select a panel off the strip."""
        state.show_tab(TAB_GRAPH)

        assert state.tab == TAB_MESSAGE

        await state.show_chip_graph(ChipKind.TAG.value, "t1", "Steuer")
        state.show_tab(TAB_MESSAGE)
        state.show_tab(TAB_GRAPH)

        assert state.tab == TAB_GRAPH

    async def test_another_message_takes_the_tab_with_it(
        self, state, digest, html_digest
    ) -> None:
        """The topic on the strip is the previous message's topic, and a
        reader who was looking at it lands on the mail rather than on a panel
        that is no longer there."""
        await state.show_chip_graph(ChipKind.TOPIC.value, "topic:1", "Nord 42")

        await state._open_message("m4@example.com", html_digest)

        assert state.chip_open is False
        assert state.chip_label == ""
        assert state.tab == TAB_MESSAGE

    async def test_the_pane_itself_draws_nothing(self, state) -> None:
        """The hook, stated as a test: the reading pane may not import the
        graph explorer, so what it does with a pressed chip is remember it.
        `mailarc_ui.graph.ChipGraphState` is what fills the panel, and
        `test_ui_graph_chips` is where that is checked."""
        assert await state._load_chip_graph() is None


class TestTheGraphGlyphs:
    """The other half of the header's way into the graph.

    A topic, a tag and a group are things the message *belongs to* and the
    header prints them as chips. A message and a person are already on the
    header — as a subject and as a name — so what they get is one glyph beside
    them. Both open the same third tab.
    """

    async def test_every_person_on_the_header_can_be_rooted_at(
        self, state, digest
    ) -> None:
        """A `To` line is a list of people and each of them has a picture of
        their own, so the line cannot be one joined string any more."""
        await state._open_message("m1@example.com", digest)

        assert state.view.recipients[0].address == "anna@example.com"
        assert state.has_cc is False

    async def test_pressing_a_person_opens_the_same_tab_a_chip_does(
        self, state
    ) -> None:
        await state.show_chip_graph(
            ChipKind.ADDRESS.value, "anna@example.com", "Anna Bauer"
        )

        assert state.chip_open is True
        assert state.chip_kind == ChipKind.ADDRESS.value
        assert state.chip_id == "anna@example.com"
        assert state.tab == TAB_GRAPH

    async def test_pressing_the_subject_roots_it_at_the_message(
        self, state, digest
    ) -> None:
        await state._open_message("m1@example.com", digest)

        await state.show_chip_graph(
            ChipKind.MESSAGE.value, state.selected_id, state.view.subject
        )

        assert state.chip_id == "m1@example.com"
        assert state.chip_label == "SwiftScan"

    def test_the_glyph_says_what_pressing_it_does(self) -> None:
        """Not what the thing is: a person glyph beside a person's name reads
        as decoration, and every one of these is the same gesture."""
        assert GRAPH_GLYPH not in CHIP_ICONS.values()


class TestTheComponents:
    """Built against a state no page of this application has — which is the
    claim: a prop appkit_mantine does not have only shows up when it is built,
    and a var the mixin did not copy only shows up when it is resolved."""

    @pytest.mark.parametrize(
        "build",
        [
            message_header,
            message_body,
            message_view,
            raw_message_view,
            remote_content_bar,
            message_tabs,
        ],
    )
    def test_builds_on_any_state_that_lists_the_mixin(self, build) -> None:
        assert isinstance(build(OtherDetailState), rx.Component)

    def test_the_pane_reads_the_state_it_was_handed(self) -> None:
        """Not the mixin's, and not the other page's: the rendered var name
        carries the concrete state's own full name."""
        rendered = str(message_tabs(OtherDetailState).render())

        assert OtherDetailState.get_full_name().replace(".", "__") in rendered

    def test_the_tabs_are_told_to_fill_their_column(self) -> None:
        """Mantine 9 lays Tabs out through CSS variables its stylesheet owns;
        only inline `styles` override them. Without this the panel is
        content-sized and the mail frame falls back to 150 pixels."""
        rendered = str(message_tabs(OtherDetailState).render())

        assert '["--tabs-display"] : "flex"' in rendered
        assert '["--tabs-panel-grow"] : "1"' in rendered
        assert '["--tabs-flex-direction"] : "column"' in rendered

    def test_the_body_frame_is_sandboxed(self) -> None:
        """The one line the whole HTML story rests on: render it and read it."""
        rendered = str(message_view(OtherDetailState).render())

        assert 'sandbox:""' in rendered
        assert "srcDoc" in rendered

    def test_the_policy_the_frame_carries_is_the_blocking_one(self) -> None:
        assert FRAME_CSP.startswith("default-src 'none'")

    def test_the_header_wears_the_archive_design(self) -> None:
        """The reading pane is styled by `assets/css/mail-archive.css`, and a
        class name is the only thing that reaches it. A rule declared there and
        named nowhere is dead CSS that no import error and no failing render
        would ever point at — so the header's own vocabulary is asserted."""
        rendered = str(message_header(OtherDetailState).render())

        for wanted in (
            "ma-reading-header",
            "ma-reading-sender",
            "ma-reading-address",
            "ma-reading-date",
            "ma-reading-subject",
            "ma-reading-recipients",
            "ma-reading-recipient-label",
            "ma-avatar",
            "ma-attachment-card",
        ):
            assert wanted in rendered, wanted

    def test_the_subject_stands_above_the_sender(self) -> None:
        """A mail client's order, and the reason the block was rearranged:
        the subject first as the largest thing on the pane, the sender and
        the people it went to underneath it."""
        rendered = str(message_header(OtherDetailState).render())

        assert rendered.index("ma-reading-subject") < rendered.index(
            "ma-reading-sender"
        )

    def test_the_recipients_are_labelled_the_way_a_client_prints_them(self) -> None:
        """`To:` and `Cc:` read inline in front of the addresses, in the text
        column under the sender — not as a label column of their own."""
        rendered = str(message_header(OtherDetailState).render())

        assert '"To:"' in rendered
        assert '"Cc:"' in rendered

    def test_the_header_hangs_a_graph_glyph_on_what_it_names(self) -> None:
        """Beside the subject, beside the sender, beside every recipient."""
        rendered = str(message_header(OtherDetailState).render())

        assert "ma-glyph-action" in rendered
        assert "in the graph" in rendered
        assert rendered.count("ma-glyph-action") >= 3

    def test_the_header_offers_the_chips_the_message_wears(self) -> None:
        rendered = str(message_header(OtherDetailState).render())

        assert "ma-chip-link" in rendered
        assert "ma-chip-inert" in rendered
        assert "ma-reading-chips" in rendered

    def test_there_are_two_tabs_until_a_panel_is_handed_in(self) -> None:
        """The reading pane cannot draw a graph, so a caller that has no panel
        to give gets exactly the strip it always had."""
        rendered = str(message_tabs(OtherDetailState).render())

        assert f'value:"{TAB_MESSAGE}"' in rendered
        assert f'value:"{TAB_SOURCE}"' in rendered
        assert f'value:"{TAB_GRAPH}"' not in rendered

    def test_the_third_tab_lands_after_the_source_one(self) -> None:
        rendered = str(
            message_tabs(OtherDetailState, graph_panel=mn.text("drawn")).render()
        )

        assert rendered.index(f'value:"{TAB_SOURCE}"') < rendered.index(
            f'value:"{TAB_GRAPH}"'
        )
        assert "drawn" in rendered

    def test_the_text_body_is_set_in_the_reading_face(self) -> None:
        """The plain-text body is the one the page draws itself; the HTML one
        is inside a frame no rule of ours can reach."""
        assert "ma-reading-body" in str(message_body(OtherDetailState).render())
