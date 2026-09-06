"""Tests for :mod:`mailarc_ui.message_detail.preview` and the panel it feeds.

Two halves, and the split is the module's own. The projection is pure — a
parsed attachment in, a frozen value object out — so most of this file needs no
archive at all and asks the questions that actually decide what a reader sees:
which of the four shapes a file lands in, and what happens to the three sizes
of file that get no shape.

The second half drives the state through a real blob store and a real
:class:`~mailarc_core.ArchiveReader`, because opening a file is a *second* read
of the stored original and that is the part worth proving: the strip lists
names, and the bytes are fetched only when somebody presses one.
"""

from collections.abc import Iterator
from typing import cast

import pytest
import reflex as rx
from appkit_commons.registry import service_registry

from mailarc_core import ArchiveReader
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import BlobKind
from mailarc_core.archive.reader import GraphSessionFactory
from mailarc_core.mail.model import ParsedAttachment
from mailarc_ui.message_detail import (
    FILE_ICONS,
    FRAME_CSP,
    NO_FILENAME,
    PREVIEW_LIMIT,
    PREVIEW_OF_KIND,
    TEXT_LIMIT,
    AttachmentPreview,
    FileKind,
    MessageDetailState,
    PreviewKind,
    attachment_panel,
    data_uri,
    message_pane,
    pretty_json,
    preview_kind,
    preview_of,
)
from mailarc_ui.message_detail.preview import (
    EMPTY_PART,
    NOT_PREVIEWABLE,
    PREVIEW_FAILED,
    TEXT_CUT,
    TOO_LARGE,
)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)
"""The first bytes of a real PNG. Nothing decodes it — what matters is that
the projection carries bytes through untouched."""


def attachment(**fields: object) -> ParsedAttachment:
    """A parsed attachment with everything but the fields under test defaulted."""
    return ParsedAttachment.model_validate(
        {"content_type": "application/octet-stream", "size": 12} | fields
    )


class TestWhichShapeAFileTakes:
    """What the panel can draw, per family.

    Which family a file *is* belongs to ``test_ui_message_filetype``; this
    asks only the question this module owns — given the family, what shape.
    """

    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("image/png", PreviewKind.IMAGE),
            ("application/pdf", PreviewKind.PDF),
            ("text/html", PreviewKind.HTML),
            ("text/plain", PreviewKind.TEXT),
            ("text/csv", PreviewKind.TEXT),
            ("application/json", PreviewKind.TEXT),
            ("text/calendar", PreviewKind.TEXT),
            ("message/rfc822", PreviewKind.TEXT),
            ("application/vnd.ms-excel", PreviewKind.NONE),
            ("application/zip", PreviewKind.NONE),
            ("audio/mpeg", PreviewKind.NONE),
        ],
    )
    def test_a_family_maps_to_one_shape(
        self, content_type: str, expected: PreviewKind
    ) -> None:
        assert preview_kind(content_type, None) is expected

    def test_two_families_that_share_a_glyph_can_still_differ_here(self) -> None:
        """The reason there are two enums: a CSV and an XLSX are both
        spreadsheet-shaped to a reader and only one of them can be drawn."""
        assert PREVIEW_OF_KIND[FileKind.TABLE] is PreviewKind.TEXT
        assert PREVIEW_OF_KIND[FileKind.SPREADSHEET] is PreviewKind.NONE

    def test_every_family_can_say_what_it_draws(self) -> None:
        """A map keyed by an enum in another module rots in silence: nothing
        type-checks it and no coverage report notices a missing entry — and a
        family with no entry here is a ``KeyError`` on somebody's mail."""
        assert set(PREVIEW_OF_KIND) == set(FileKind)


class TestTurningBytesIntoSomethingAnAttributeCanHold:
    def test_a_data_uri_carries_the_type_and_the_bytes(self) -> None:
        assert data_uri("image/png", b"abc") == "data:image/png;base64,YWJj"

    def test_a_file_with_no_type_still_gets_a_uri(self) -> None:
        assert data_uri("", b"abc").startswith("data:application/octet-stream;base64,")

    def test_json_is_laid_out_over_lines(self) -> None:
        assert pretty_json('{"a":[1,2]}') == '{\n  "a": [\n    1,\n    2\n  ]\n}'

    def test_umlauts_survive_the_layout(self) -> None:
        assert pretty_json('{"a":"Grüße"}') == '{\n  "a": "Grüße"\n}'

    def test_what_is_not_json_is_left_exactly_as_it_arrived(self) -> None:
        """A broken export is still worth looking at, and often the reason."""
        assert pretty_json("id;total\n1;9,90") == "id;total\n1;9,90"


class TestWhatThePanelIsHanded:
    """:func:`preview_of` — one attachment, projected."""

    def test_a_picture_arrives_as_a_source(self) -> None:
        found = preview_of(
            attachment(filename="logo.png", content_type="image/png", payload=PNG)
        )
        assert found.kind == PreviewKind.IMAGE.value
        assert found.source.startswith("data:image/png;base64,")
        assert found.document == found.text == ""

    def test_a_pdf_arrives_as_a_source_too(self) -> None:
        found = preview_of(
            attachment(
                filename="Rechnung.pdf",
                content_type="application/pdf",
                payload=b"%PDF-1.4",
            )
        )
        assert found.kind == PreviewKind.PDF.value
        assert found.source.startswith("data:application/pdf;base64,")

    def test_html_arrives_framed_under_the_policy_that_blocks_the_network(
        self,
    ) -> None:
        """The same defence the mail body gets: an attachment is somebody
        else's markup too, and a tracking pixel in one counts the same."""
        found = preview_of(
            attachment(content_type="text/html", payload=b"<p>Hallo</p>")
        )
        assert found.kind == PreviewKind.HTML.value
        assert FRAME_CSP in found.document
        assert "<p>Hallo</p>" in found.document

    def test_text_arrives_as_text(self) -> None:
        found = preview_of(
            attachment(content_type="text/plain", payload="Grüße\nJens".encode())
        )
        assert found.kind == PreviewKind.TEXT.value
        assert found.text == "Grüße\nJens"
        assert found.note == ""

    def test_a_json_attachment_is_laid_out_on_the_way_through(self) -> None:
        found = preview_of(
            attachment(content_type="application/json", payload=b'{"a":1}')
        )
        assert found.text == '{\n  "a": 1\n}'

    def test_a_file_wears_its_family_glyph_and_not_the_preview_shape(
        self,
    ) -> None:
        """A spreadsheet and a zip are both undrawable and are not the same
        thing; the panel that cannot show either still says which it has."""
        found = preview_of(
            attachment(
                filename="Umsatz.xlsx",
                content_type="application/vnd.ms-excel",
                payload=b"x",
            )
        )
        assert found.kind == PreviewKind.NONE.value
        assert found.icon == FILE_ICONS[FileKind.SPREADSHEET]
        assert found.icon != FILE_ICONS[FileKind.OTHER]

    def test_a_file_nothing_can_draw_says_so_and_still_names_itself(self) -> None:
        found = preview_of(
            attachment(
                filename="Umsatz.xlsx",
                content_type="application/vnd.ms-excel",
                size=2048,
                payload=b"PK\x03\x04",
            )
        )
        assert found.kind == PreviewKind.NONE.value
        assert found.note == NOT_PREVIEWABLE
        assert (found.filename, found.size_label) == ("Umsatz.xlsx", "2.0 KB")

    def test_a_nameless_part_is_called_the_same_thing_the_strip_calls_it(
        self,
    ) -> None:
        assert preview_of(attachment(payload=b"x")).filename == NO_FILENAME

    def test_a_part_with_no_stored_bytes_says_that_rather_than_nothing(self) -> None:
        found = preview_of(
            attachment(filename="ghost.pdf", content_type="application/pdf")
        )
        assert (found.kind, found.note) == (PreviewKind.NONE.value, EMPTY_PART)

    def test_a_file_past_the_limit_is_not_pushed_through_the_socket(self) -> None:
        """Every previewable kind travels inline, so the cap is the whole cost
        control — a fifty-megabyte video would otherwise be base64 on a wire."""
        found = preview_of(
            attachment(
                filename="film.png",
                content_type="image/png",
                size=PREVIEW_LIMIT + 1,
                payload=b"x" * (PREVIEW_LIMIT + 1),
            )
        )
        assert (found.kind, found.note) == (PreviewKind.NONE.value, TOO_LARGE)
        assert found.source == ""

    def test_a_long_text_is_cut_and_says_it_was(self) -> None:
        found = preview_of(
            attachment(content_type="text/plain", payload=b"a" * (TEXT_LIMIT + 10))
        )
        assert len(found.text) == TEXT_LIMIT
        assert found.note == TEXT_CUT


RAW_WITH_FILES = (
    b"From: shop@example.com\r\n"
    b"To: jens@example.com\r\n"
    b"Subject: Rechnung\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
    b"--B\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Die Rechnung im Anhang.\r\n"
    b"--B\r\n"
    b"Content-Type: text/csv; charset=utf-8\r\n"
    b'Content-Disposition: attachment; filename="posten.csv"\r\n'
    b"\r\n"
    b"artikel;preis\r\nKaffee;9,90\r\n"
    b"--B\r\n"
    b"Content-Type: application/pdf\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b'Content-Disposition: attachment; filename="Rechnung.pdf"\r\n'
    b"\r\n"
    b"JVBERi0xLjQK\r\n"
    b"--B--\r\n"
)
"""One mail with two files on it, which is what makes an index worth testing."""


class OtherDetailState(MessageDetailState, rx.State):
    """A page the application does not have — the mixin and nothing else."""


class TrustSession:
    """A ``runic.ogm.Session`` stand-in with nothing in it."""

    def get(self, cls: type, pk: str) -> None:
        return None

    def __enter__(self) -> TrustSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def blobs(tmp_path) -> BlobStore:
    return BlobStore(ArchiveConfig(store_dir=tmp_path / "blobs"))


@pytest.fixture
def digest(blobs: BlobStore) -> str:
    return blobs.put(RAW_WITH_FILES, BlobKind.MESSAGE)


@pytest.fixture
def published(blobs: BlobStore) -> Iterator[ArchiveReader]:
    registry = service_registry()
    saved = registry.snapshot()
    reader = ArchiveReader(cast(GraphSessionFactory, TrustSession), blobs)
    registry.register_as(ArchiveReader, reader)
    yield reader
    registry.restore(saved)


@pytest.fixture
async def opened(published: ArchiveReader, digest: str) -> OtherDetailState:
    """A state with that message open and its two files listed."""
    state = OtherDetailState()
    await state._open_message("m1", digest)
    return state


class TestOpeningOneFile:
    async def test_the_strip_lists_the_files_without_carrying_their_bytes(
        self, opened: OtherDetailState
    ) -> None:
        """The whole reason the digest is kept: what crosses on selection is
        two names and two sizes, not two files."""
        assert [one.filename for one in opened.view.attachments] == [
            "posten.csv",
            "Rechnung.pdf",
        ]
        assert opened.eml_sha256 != ""
        assert not opened.preview_open

    async def test_pressing_the_second_card_opens_the_second_file(
        self, opened: OtherDetailState
    ) -> None:
        """Addressed by position: a mail with two ``image001.png`` parts is
        routine, and a lookup by name would open whichever came first."""
        await opened.show_attachment(1)
        assert opened.preview_open
        assert opened.preview.filename == "Rechnung.pdf"
        assert opened.preview.kind == PreviewKind.PDF.value
        assert opened.preview.source.startswith("data:application/pdf;base64,")
        assert not opened.preview_loading

    async def test_a_text_file_comes_back_readable(
        self, opened: OtherDetailState
    ) -> None:
        await opened.show_attachment(0)
        assert opened.preview.kind == PreviewKind.TEXT.value
        assert "Kaffee;9,90" in opened.preview.text

    async def test_an_index_from_nowhere_opens_nothing(
        self, opened: OtherDetailState
    ) -> None:
        """The index arrives over the socket, so it is checked against what is
        on screen rather than trusted."""
        for index in (-1, 2, 99):
            await opened.show_attachment(index)
            assert not opened.preview_open

    async def test_a_missing_original_becomes_a_sentence_and_not_a_broken_page(
        self, opened: OtherDetailState
    ) -> None:
        """The panel is already up when the read happens — it has to be able
        to say why it is empty."""
        opened.eml_sha256 = "0" * 64
        await opened.show_attachment(0)
        assert opened.preview_open
        assert opened.preview.note == PREVIEW_FAILED
        assert opened.preview.filename == "posten.csv"
        assert not opened.preview_loading

    async def test_an_original_that_no_longer_holds_that_file_says_so(
        self, opened: OtherDetailState, blobs: BlobStore
    ) -> None:
        """The strip and the bytes are two reads of the same message, and
        nothing holds them together in between: a re-parse that comes back
        with fewer parts than the strip listed has to land on a sentence
        rather than on an index error."""
        opened.eml_sha256 = blobs.put(b"Subject: leer\r\n\r\n", BlobKind.MESSAGE)
        await opened.show_attachment(1)
        assert opened.preview.note == PREVIEW_FAILED
        assert opened.preview.source == ""

    async def test_a_read_that_throws_is_a_sentence_too(
        self, opened: OtherDetailState, published: ArchiveReader, monkeypatch
    ) -> None:
        """A blob store that is gone mid-session is the case the panel cannot
        anticipate — and the one where a page that raised would take the whole
        reading pane down with it."""

        def boom(_digest: str) -> bytes:
            raise OSError("the store went away")

        monkeypatch.setattr(published, "raw_message", boom)
        await opened.show_attachment(0)
        assert opened.preview_open
        assert opened.preview.note == PREVIEW_FAILED
        assert not opened.preview_loading

    async def test_closing_keeps_what_was_shown_until_the_next_file(
        self, opened: OtherDetailState
    ) -> None:
        """The drawer animates out; a panel that emptied itself first would
        blink to nothing under the closing animation."""
        await opened.show_attachment(0)
        opened.close_attachment()
        assert not opened.preview_open
        assert opened.preview.filename == "posten.csv"

    async def test_opening_another_message_puts_the_panel_away(
        self, opened: OtherDetailState, blobs: BlobStore
    ) -> None:
        """A file belongs to the message it came on; leaving it up over the
        next one would be showing the wrong archive entry."""
        await opened.show_attachment(0)
        await opened._open_message(
            "m2", blobs.put(b"Subject: x\r\n\r\n", BlobKind.MESSAGE)
        )
        assert not opened.preview_open
        assert opened.preview == AttachmentPreview()

    async def test_the_note_only_shows_when_there_is_one(
        self, opened: OtherDetailState
    ) -> None:
        await opened.show_attachment(0)
        assert not opened.preview_note_shown
        opened.preview = opened.preview.model_copy(update={"note": TOO_LARGE})
        assert opened.preview_note_shown


class TestThePanelItself:
    def test_the_pane_carries_the_drawer_beside_the_tabs(self) -> None:
        """Beside and not inside: the tabs unmount the panel that is not
        selected, and a file opened on the message tab has to survive a look
        at the source."""
        assert message_pane(OtherDetailState).render() is not None

    def test_the_drawer_can_be_built_against_any_state_listing_the_mixin(
        self,
    ) -> None:
        assert attachment_panel(OtherDetailState).render() is not None
