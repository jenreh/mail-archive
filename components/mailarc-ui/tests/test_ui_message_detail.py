"""Tests for :mod:`mailarc_ui.message_detail`.

The point of this package is that a second page can read a mail without
inheriting the first page's list, so every test here drives a state the
application does not have: :class:`OtherDetailState`, which lists the mixin and
brings nothing else. What it proves is that the vars, the computed vars and the
handlers materialise on *it* — not only on ``MessageReviewState``, which would
also be true of a plain base class — and that two such states hold two separate
open messages.

The reading itself runs against the real :class:`ArchiveReader` and a real blob
store, with only the graph session faked, the way ``mailarc-core``'s own reader
tests drive it. The formatting functions need neither and are checked directly.
"""

from collections.abc import Iterator
from typing import cast

import pytest
import reflex as rx
from appkit_commons.registry import service_registry

from mailarc_core import ArchiveReader
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import Address, BlobKind
from mailarc_core.archive.reader import GraphSessionFactory
from mailarc_ui.message_detail import (
    FRAME_CSP,
    TAB_MESSAGE,
    TAB_SOURCE,
    MessageDetailState,
    decode_raw,
    frame_document,
    message_body,
    message_header,
    message_tabs,
    message_view,
    raw_message_view,
    remote_content_bar,
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
    """A page that is not the review page — the whole argument for the mixin.

    Empty on purpose: whatever it can do, it got from the mixin.
    """


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
def published(session: TrustSession, blobs: BlobStore) -> Iterator[ArchiveReader]:
    """The reader, left where the composition root would leave it."""
    registry = service_registry()
    saved = registry.snapshot()
    reader = ArchiveReader(cast(GraphSessionFactory, lambda: session), blobs)
    registry.register_as(ArchiveReader, reader)
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
        """The reason for a mixin rather than one shared substate: the search
        page and the review page each keep their own selection."""
        from mailarc_ui.review import MessageReviewState

        other = MessageReviewState()
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
        assert state.view.sender == "Jens <jens@example.com>"
        assert state.view.body_text == "Erstellt mit SwiftScan.\n"

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
    """Built against a state the review page never heard of — which is the
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
            "ma-avatar",
            "ma-attachment-card",
        ):
            assert wanted in rendered, wanted

    def test_the_text_body_is_set_in_the_reading_face(self) -> None:
        """The plain-text body is the one the page draws itself; the HTML one
        is inside a frame no rule of ours can reach."""
        assert "ma-reading-body" in str(message_body(OtherDetailState).render())
