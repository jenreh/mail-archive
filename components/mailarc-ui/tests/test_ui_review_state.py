"""Tests for :mod:`mailarc_ui.review.state`.

Against the real :class:`ArchiveReader` and a real blob store; only the graph
session behind the reader is a fake handing back canned nodes, the way
``mailarc-core``'s own reader tests drive it. Both claims worth proving here
are about the seams: that the state finds its reader where the composition
root left it, and that what a row shows is what the reader read.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import reflex as rx
from appkit_commons.registry import service_registry

from mailarc_core import ArchiveReader
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import Address, BlobKind, Label, Message
from mailarc_core.archive.reader import GraphSessionFactory
from mailarc_core.mail.model import EmailAddress, LabelKind
from mailarc_ui.review import (
    LabelChip,
    MessageReviewState,
    MessageRow,
    MessageView,
    archive_reader,
    date_label,
    decode_raw,
    frame_document,
    message_list,
    message_tabs,
    message_view,
    raw_message_view,
    review_panel,
)
from mailarc_ui.review.state import (
    FRAME_CSP,
    NO_SUBJECT,
    PAGE_SIZE,
    TAB_MESSAGE,
    TAB_SOURCE,
    YESTERDAY,
    address_label,
    label_text,
    long_date_label,
    size_label,
)

NOW = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)
RAW = (
    b"From: Jens <jens@example.com>\r\n"
    b"To: Anna Bauer <anna@example.com>, bob@example.com\r\n"
    b"Cc: carl@example.com\r\n"
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
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
    b"--B\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>Lieber <b>Gast</b></p><script>alert(1)</script>"
    b'<img src="https://tracker.example/pixel.gif">\r\n'
    b"--B\r\n"
    b'Content-Type: application/pdf; name="Rechnung.pdf"\r\n'
    b'Content-Disposition: attachment; filename="Rechnung.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"JVBERi0xLjQK\r\n"
    b"--B--\r\n"
)


class FakeSession:
    """A `runic.ogm.Session` stand-in that lists what the test put in.

    Two statements reach it — the listing, and the label lookup for the
    page, told apart by the edge it walks — and it answers each with the
    rows the test put in for it.
    """

    def __init__(
        self,
        rows: list[tuple[Message, Address | Label | None]],
        labels: list[tuple[Message, Address | Label | None]] | None = None,
    ) -> None:
        self.rows = rows
        self.labels = labels or []
        self.listed: list[tuple[int, int]] = []
        self.addresses: dict[str, Address] = {}

    def get(self, cls: type, pk: str) -> Address | None:
        """What the trust lookup calls: the node under this key, or nothing."""
        assert cls is Address
        return self.addresses.get(pk)

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def all_with_edges(self, statement) -> list[tuple[Message, Address | Label | None]]:
        cypher, _ = statement.build()
        if "LABELED" in cypher:
            return self.labels
        self.listed.append((statement._skip_val, statement._limit_val))  # noqa: SLF001
        skip, limit = self.listed[-1]
        return self.rows[skip : skip + limit]

    def count(self, _statement) -> int:
        return len(self.rows)


def message(number: int, **overrides: Any) -> Message:
    fields: dict[str, Any] = {
        "id": f"m{number}@example.com",
        "subject": f"SwiftScan {number}",
        "sent_at": NOW - timedelta(days=number),
        "body_text": "Erstellt mit SwiftScan.",
        "has_attachments": number % 2 == 0,
    }
    return Message(**{**fields, **overrides})


def sender() -> Address:
    return Address(id="jens@example.com", display_names=["Jens Rehpöhler"])


def label(name: str, kind: LabelKind = LabelKind.USER) -> Label:
    return Label(id=f"7:{name}", name=name, kind=kind)


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
def session(digest: str, html_digest: str) -> FakeSession:
    return FakeSession(
        [
            (message(1, eml_sha256=digest), sender()),
            (message(2, subject=None), None),
            (message(3, eml_sha256="0" * 64), sender()),
            (message(4, eml_sha256=html_digest), None),
        ],
        labels=[
            (message(1), label("CATEGORY_PROMOTIONS", LabelKind.SYSTEM)),
            (message(1), label("Kunden/Bauer")),
            (message(1), label("Archiv 2025", LabelKind.FOLDER)),
        ],
    )


@pytest.fixture
def published(session: FakeSession, blobs: BlobStore) -> Iterator[ArchiveReader]:
    """The reader, left where the composition root would leave it."""
    registry = service_registry()
    saved = registry.snapshot()
    reader = ArchiveReader(cast(GraphSessionFactory, lambda: session), blobs)
    registry.register_as(ArchiveReader, reader)
    yield reader
    registry.restore(saved)


@pytest.fixture
def state(published: ArchiveReader) -> MessageReviewState:
    return MessageReviewState()


class TestFindingTheReader:
    def test_the_published_reader_is_the_one_the_state_uses(self, published) -> None:
        assert archive_reader() is published

    def test_an_unpublished_reader_is_a_sentence_not_a_key_error(self) -> None:
        registry = service_registry()
        saved = registry.snapshot()
        try:
            registry.restore({})
            with pytest.raises(RuntimeError, match=r"app\.composition"):
                archive_reader()
        finally:
            registry.restore(saved)


class TestLoading:
    async def test_load_lists_the_archive_newest_first(self, state) -> None:
        await state.load()

        assert state.error == ""
        assert state.total == 4
        assert [row.id for row in state.messages] == [
            "m1@example.com",
            "m2@example.com",
            "m3@example.com",
            "m4@example.com",
        ]
        assert state.has_messages is True
        assert state.has_more is False
        assert state.count_label == "4"

    async def test_a_row_is_what_the_reader_read_made_printable(
        self, state, digest
    ) -> None:
        await state.load()

        first, second, _, _ = state.messages
        assert first.sender == "Jens Rehpöhler"
        assert first.subject == "SwiftScan 1"
        assert first.preview == "Erstellt mit SwiftScan."
        assert first.eml_sha256 == digest
        assert first.has_attachments is False
        assert second.sender == ""
        assert second.subject == NO_SUBJECT
        assert second.has_attachments is True
        assert second.eml_sha256 == ""

    async def test_a_row_wears_its_labels_as_chips(self, state) -> None:
        """In the reader's order — a human's first — each coloured by kind,
        and the provider's own names made readable."""
        await state.load()

        first, second, _, _ = state.messages
        assert first.labels == [
            LabelChip(text="Kunden/Bauer", color="blue"),
            LabelChip(text="Archiv 2025", color="teal"),
            LabelChip(text="Promotions", color="gray"),
        ]
        assert second.labels == []

    async def test_a_reader_that_fails_leaves_a_message_not_a_traceback(
        self, state, session
    ) -> None:
        def boom(*_args, **_kwargs):
            raise ConnectionError("graph is down")

        session.all_with_edges = boom

        await state.load()

        assert state.error == "graph is down"
        assert state.loading is False
        assert state.messages == []

    async def test_reloading_keeps_a_selection_that_still_exists(self, state) -> None:
        await state.load()
        await state.select("m1@example.com")

        await state.load()

        assert state.selected_id == "m1@example.com"

    async def test_reloading_drops_a_selection_that_is_gone(
        self, state, session
    ) -> None:
        await state.load()
        await state.select("m3@example.com")
        session.rows = session.rows[:1]

        await state.load()

        assert state.selected_id == ""
        assert state.raw == ""
        assert state.view == MessageView()


class TestPaging:
    async def test_the_first_load_is_one_page(self, state, session) -> None:
        session.rows = [(message(n), None) for n in range(PAGE_SIZE + 5)]

        await state.load()

        assert len(state.messages) == PAGE_SIZE
        assert state.has_more is True
        assert state.count_label == f"{PAGE_SIZE} of {PAGE_SIZE + 5}"

    async def test_load_more_appends_the_next_page(self, state, session) -> None:
        session.rows = [(message(n), None) for n in range(PAGE_SIZE + 5)]
        await state.load()

        await state.load_more()

        assert len(state.messages) == PAGE_SIZE + 5
        assert session.listed[-1] == (PAGE_SIZE, PAGE_SIZE)
        assert state.has_more is False

    async def test_load_more_does_nothing_when_everything_is_shown(
        self, state, session
    ) -> None:
        await state.load()
        before = list(session.listed)

        await state.load_more()

        assert session.listed == before


class TestSelecting:
    async def test_selecting_reads_the_original_by_its_digest(self, state) -> None:
        await state.load()

        await state.select("m1@example.com")

        assert state.selected_id == "m1@example.com"
        assert state.has_selection is True
        assert state.raw == RAW.decode()
        assert state.raw_truncated is False
        assert state.loading_raw is False

    async def test_selecting_renders_the_readable_view(self, state) -> None:
        await state.load()

        await state.select("m1@example.com")

        view = state.view
        assert view.subject == "SwiftScan"
        assert view.sender == "Jens <jens@example.com>"
        assert view.recipients == "Anna Bauer <anna@example.com>, bob@example.com"
        assert view.cc == "carl@example.com"
        assert view.date.endswith(
            datetime(2026, 8, 19, 14, 28, tzinfo=UTC).astimezone().strftime("%H:%M")
        )
        assert view.body_html == ""
        assert view.body_text == "Erstellt mit SwiftScan.\n"
        assert view.attachments == []
        assert state.has_html_body is False
        assert state.has_cc is True
        assert state.has_attachments is False

    async def test_an_html_mail_is_framed_with_its_files_listed(self, state) -> None:
        """The markup is kept whole — a script tag included — because the
        sandbox, not a tag list, is what disarms it; the frame document says so
        with its policy."""
        await state.load()

        await state.select("m4@example.com")

        view = state.view
        assert "<p>Lieber <b>Gast</b></p><script>alert(1)</script>" in view.body_html
        assert state.frame_html.startswith("<!DOCTYPE html>")
        assert FRAME_CSP in state.frame_html
        assert [a.filename for a in view.attachments] == ["Rechnung.pdf"]
        assert view.attachments[0].size_label == "9 B"
        assert state.has_html_body is True
        assert state.has_attachments is True

    async def test_remote_content_starts_blocked(self, state) -> None:
        await state.load()

        await state.select("m4@example.com")

        assert state.view.remote_references == 1
        assert state.remote_allowed is False
        assert state.remote_blocked is True
        assert "1 remote reference" in state.remote_notice
        assert "img-src data:;" in state.frame_html
        assert "https:" not in state.frame_html.split("</head>")[0].split("<style>")[0]

    async def test_a_mail_without_remote_content_asks_nothing(self, state) -> None:
        await state.load()

        await state.select("m1@example.com")

        assert state.view.remote_references == 0
        assert state.remote_blocked is False

    async def test_allow_once_opens_this_render_only(self, state) -> None:
        await state.load()
        await state.select("m4@example.com")

        state.allow_remote_once()

        assert state.remote_blocked is False
        assert "img-src data: https: http:" in state.frame_html
        # Nothing was recorded: the next selection of the same mail asks again.
        await state.select("m2@example.com")
        await state.select("m4@example.com")
        assert state.remote_blocked is True

    async def test_allow_for_sender_is_recorded_on_the_address(
        self, state, session
    ) -> None:
        """The decision lands on the graph's Address node, so it holds for the
        next message from the same sender — and the next session."""
        session.addresses["shop@example.com"] = Address(id="shop@example.com")
        await state.load()
        await state.select("m4@example.com")

        await state.allow_remote_for_sender()

        assert state.remote_blocked is False
        assert session.addresses["shop@example.com"].remote_trusted is True
        assert state.message_note == ""
        # A later selection of the same sender's mail is open from the start.
        await state.select("m2@example.com")
        await state.select("m4@example.com")
        assert state.remote_allowed is True
        assert state.remote_blocked is False

    async def test_a_sender_the_archive_does_not_know_still_allows_once(
        self, state
    ) -> None:
        """No Address node to write on — the mail still opens, with a note."""
        await state.load()
        await state.select("m4@example.com")

        await state.allow_remote_for_sender()

        assert state.remote_allowed is True
        assert "allowed once" in state.message_note

    async def test_a_message_without_a_stored_original_says_so(self, state) -> None:
        await state.load()

        await state.select("m2@example.com")

        assert state.selected_id == "m2@example.com"
        assert "No original" in state.raw
        assert "No original" in state.view.body_text

    async def test_a_missing_blob_says_so(self, state) -> None:
        await state.load()

        await state.select("m3@example.com")

        assert "missing" in state.raw
        assert "missing" in state.view.body_text

    async def test_a_new_selection_forgets_the_previous_view(self, state) -> None:
        await state.load()
        await state.select("m4@example.com")

        await state.select("m2@example.com")

        assert state.view.attachments == []
        assert state.view.body_html == ""


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

    async def test_the_tab_outlives_the_selection(self, state) -> None:
        await state.load()
        state.show_tab(TAB_SOURCE)

        await state.select("m1@example.com")

        assert state.tab == TAB_SOURCE

    async def test_an_unknown_id_is_ignored(self, state) -> None:
        await state.load()

        await state.select("nobody@example.com")

        assert state.selected_id == ""


class TestTheDateLabel:
    def test_today_is_a_time(self) -> None:
        sent = NOW - timedelta(hours=2)

        assert date_label(sent, NOW) == sent.astimezone().strftime("%H:%M")

    def test_yesterday_is_a_word(self) -> None:
        yesterday = NOW.astimezone() - timedelta(days=1)

        assert date_label(yesterday, NOW) == YESTERDAY

    def test_anything_older_is_a_short_date(self) -> None:
        assert date_label(datetime(2026, 8, 4, 12, 0, tzinfo=UTC), NOW) == "04.08.26"

    def test_no_date_is_no_label(self) -> None:
        assert date_label(None, NOW) == ""


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
        spam trick besides. In a list row that has to be one empty cell, not a
        message list that will not render.
        """
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        far_future = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

        assert date_label(far_future, NOW) == ""
        assert long_date_label(far_future) == ""

    def test_the_other_end_of_the_range_costs_one_cell_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Year 0001 overflows west of UTC, so the guard is two-sided."""
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        long_ago = datetime(1, 1, 1, tzinfo=UTC)

        assert date_label(long_ago, NOW) == ""
        assert long_date_label(long_ago) == ""

    @pytest.mark.parametrize(
        ("size", "label"),
        [(0, "0 B"), (512, "512 B"), (12 * 1024, "12 KB"), (1400 * 1024, "1.4 MB")],
    )
    def test_sizes_read_like_a_file_list(self, size: int, label: str) -> None:
        assert size_label(size) == label


class TestTheFrameDocument:
    def test_the_policy_comes_before_the_mail(self) -> None:
        document = frame_document("<p>hi</p>")

        assert document.index("Content-Security-Policy") < document.index("<p>hi</p>")
        assert "default-src 'none'" in document
        assert "img-src data:" in document


class TestDecodingTheRaw:
    def test_utf8_comes_through_whole(self) -> None:
        assert decode_raw("Grüße".encode()) == ("Grüße", False)

    def test_a_mislabelled_byte_is_replaced_not_fatal(self) -> None:
        text, truncated = decode_raw(b"Gr\xfc\xdfe")

        assert text == "Gr��e"
        assert truncated is False

    def test_a_long_original_is_cut_after_decoding(self) -> None:
        text, truncated = decode_raw(("é" * 10).encode(), limit=4)

        assert text == "éééé"
        assert truncated is True


class TestTheComponents:
    """A prop appkit_mantine does not have only shows up when it is built."""

    @pytest.mark.parametrize(
        "build",
        [message_list, message_view, message_tabs, raw_message_view, review_panel],
    )
    def test_builds(self, build) -> None:
        assert isinstance(build(), rx.Component)

    def test_the_tabs_are_told_to_fill_their_column(self) -> None:
        """Mantine 9 lays Tabs out through CSS variables its stylesheet owns;
        only inline `styles` override them. Without this the panel is
        content-sized and the mail frame falls back to 150 pixels."""
        rendered = str(message_tabs().render())

        assert '["--tabs-display"] : "flex"' in rendered
        assert '["--tabs-panel-grow"] : "1"' in rendered
        assert '["--tabs-flex-direction"] : "column"' in rendered

    def test_the_body_frame_is_sandboxed(self) -> None:
        """The one line the whole HTML story rests on: render it and read it."""
        rendered = str(message_view().render())

        assert 'sandbox:""' in rendered
        assert "srcDoc" in rendered

    def test_a_row_is_frozen(self) -> None:
        row = MessageRow(id="m", sender="", subject="", preview="", date_label="")

        with pytest.raises(Exception, match="frozen"):
            row.sender = "x"  # ty: ignore[invalid-assignment]
