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

import base64
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
    calendar_preview,
    contact_preview,
    data_uri,
    message_pane,
    pretty_json,
    preview_kind,
    preview_of,
)
from mailarc_ui.message_detail.invite import EVENT_LIMIT
from mailarc_ui.message_detail.preview import (
    CARDS_CUT,
    EMPTY_PART,
    EVENTS_CUT,
    NOT_PREVIEWABLE,
    OLD_OFFICE,
    PREVIEW_FAILED,
    TEXT_CUT,
    TOO_LARGE,
    encoded_bytes,
)
from mailarc_ui.message_detail.vcard import CARD_LIMIT

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)
"""The first bytes of a real PNG. Nothing decodes it — what matters is that
the projection carries bytes through untouched."""


ICS = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "SUMMARY:Sprint Review",
        "DTSTART:20260910T120000Z",
        "DTEND:20260910T130000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
)
"""One real invitation — the file this feature exists for."""

VCF = "\r\n".join(
    [
        "BEGIN:VCARD",
        "VERSION:3.0",
        "FN:Anna Bauer",
        "EMAIL:anna@example.com",
        "END:VCARD",
    ]
)
"""One real contact card."""


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
            ("text/calendar", PreviewKind.CALENDAR),
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
        spreadsheet-shaped to a reader and they are drawn by different things.

        A CSV is printed as text — it is text — and an XLSX goes to a
        workbook renderer that would make nonsense of a CSV.
        """
        assert PREVIEW_OF_KIND[FileKind.TABLE] is PreviewKind.TEXT
        assert PREVIEW_OF_KIND[FileKind.SPREADSHEET] is PreviewKind.SPREADSHEET

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


OOXML = {
    PreviewKind.DOCUMENT: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "Vertrag.docx",
    ),
    PreviewKind.SPREADSHEET: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Umsatz.xlsx",
    ),
    PreviewKind.SLIDES: (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "Pitch.pptx",
    ),
}
"""One real content type and name per Office shape, so a test can say which
renderer it means without repeating a forty-character MIME string."""


class TestTheOfficeFormatsAViewerCanOpen:
    """The three families that used to land on a sentence and now do not.

    The narrowing is the whole of what is interesting here: the family table
    says ``SLIDES``, and only a file that is really OOXML gets a renderer.
    """

    @pytest.mark.parametrize("expected", sorted(OOXML))
    def test_a_modern_office_file_reaches_its_renderer(
        self, expected: PreviewKind
    ) -> None:
        content_type, filename = OOXML[expected]
        found = preview_of(
            attachment(
                filename=filename, content_type=content_type, payload=b"PK\x03\x04"
            )
        )
        assert found.kind == expected.value
        assert base64.b64decode(found.encoded) == b"PK\x03\x04"

    @pytest.mark.parametrize(
        ("content_type", "filename"),
        [
            ("application/msword", "Vertrag.doc"),
            ("application/vnd.ms-excel", "Umsatz.xls"),
            ("application/vnd.ms-powerpoint", "Pitch.ppt"),
            ("application/vnd.oasis.opendocument.text", "Brief.odt"),
            ("application/octet-stream", "Keynote.key"),
            ("application/octet-stream", "Brief.pages"),
        ],
    )
    def test_an_office_family_that_is_not_ooxml_gets_no_renderer(
        self, content_type: str, filename: str
    ) -> None:
        """The bug this narrowing exists to prevent.

        Every one of these lands on an Office *family*, and handing any of
        them to a parser that reads zipped OOXML would fail somewhere inside
        a WebAssembly module rather than on this page.
        """
        found = preview_of(
            attachment(
                filename=filename, content_type=content_type, payload=b"\xd0\xcf"
            )
        )
        assert found.kind == PreviewKind.NONE.value
        assert found.note == OLD_OFFICE
        assert found.encoded == ""

    @pytest.mark.parametrize("kind", sorted(OOXML))
    def test_an_office_file_over_the_limit_carries_no_bytes(
        self, kind: PreviewKind
    ) -> None:
        """The cost control, asserted for the kinds that made it matter.

        These four renderers are the reason the limit is load-bearing rather
        than a nicety: a workbook decodes into a WebAssembly heap that copies
        it again, so the bytes that never leave the server are the cheapest
        ones in the system.
        """
        content_type, filename = OOXML[kind]
        found = preview_of(
            attachment(
                filename=filename,
                content_type=content_type,
                payload=b"\x00" * (PREVIEW_LIMIT + 1),
            )
        )
        assert (found.kind, found.note) == (PreviewKind.NONE.value, TOO_LARGE)
        assert found.encoded == ""


class TestExactlyOneWayOfHoldingContent:
    """The rule :class:`AttachmentPreview` states in prose and nothing checked.

    Six fields, one filled. It is what lets the panel be a chain of
    ``rx.cond`` over one string instead of a chain guarding against absence,
    and a kind that filled two of them would break the panel in the browser
    and nowhere else.
    """

    CONTENT = ("source", "document", "text", "encoded", "events", "contacts")

    @pytest.mark.parametrize(
        ("content_type", "filename"),
        [
            ("image/png", "bild.png"),
            ("application/pdf", "Rechnung.pdf"),
            ("text/html", "seite.html"),
            ("text/plain", "notiz.txt"),
            *OOXML.values(),
        ],
    )
    def test_one_field_and_only_one_carries_the_file(
        self, content_type: str, filename: str
    ) -> None:
        found = preview_of(
            attachment(
                filename=filename, content_type=content_type, payload=b"PK\x03\x04"
            )
        )
        filled = [name for name in self.CONTENT if getattr(found, name)]
        assert len(filled) == 1, f"{filename} filled {filled}"

    @pytest.mark.parametrize(
        ("content_type", "filename", "payload"),
        [("text/calendar", "Termin.ics", ICS), ("text/vcard", "Anna.vcf", VCF)],
    )
    def test_a_record_kind_fills_its_list_and_nothing_else(
        self, content_type: str, filename: str, payload: str
    ) -> None:
        """The two shapes that are read rather than rendered obey the same
        rule, and it is easier to break here: they decode the bytes to text on
        the way and could plausibly keep it."""
        found = preview_of(
            attachment(
                filename=filename,
                content_type=content_type,
                payload=payload.encode(),
            )
        )
        filled = [name for name in self.CONTENT if getattr(found, name)]
        assert len(filled) == 1, f"{filename} filled {filled}"

    def test_a_file_that_cannot_be_drawn_carries_nothing(self) -> None:
        found = preview_of(
            attachment(
                filename="Belege.zip", content_type="application/zip", payload=b"PK"
            )
        )
        assert [name for name in self.CONTENT if getattr(found, name)] == []

    @pytest.mark.parametrize("payload", [b"", b"\x00\xff\xfe", b"a", b"ab", b"abc"])
    def test_the_bytes_survive_the_round_trip(self, payload: bytes) -> None:
        """Padding and high bytes both, because base64 gets exactly those two
        things wrong when somebody reaches for a text codec."""
        assert base64.b64decode(encoded_bytes(payload)) == payload


class TestWhatThePanelIsHanded:
    """:func:`preview_of` — one attachment, projected."""

    def test_a_picture_arrives_as_a_source(self) -> None:
        found = preview_of(
            attachment(filename="logo.png", content_type="image/png", payload=PNG)
        )
        assert found.kind == PreviewKind.IMAGE.value
        assert found.source.startswith("data:image/png;base64,")
        assert found.document == found.text == ""

    def test_a_pdf_arrives_as_encoded_bytes(self) -> None:
        found = preview_of(
            attachment(
                filename="Rechnung.pdf",
                content_type="application/pdf",
                payload=b"%PDF-1.4",
            )
        )
        assert found.kind == PreviewKind.PDF.value
        assert base64.b64decode(found.encoded) == b"%PDF-1.4"
        assert found.source == "", "the viewer takes bytes; a URI would double them"

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

    def test_an_older_office_file_is_told_which_formats_do_work(self) -> None:
        """A Word 97 or Excel 5 file is the one undrawable case a reader can
        do something about, so it gets the sentence naming the three that
        work rather than the flat "no way to show this"."""
        found = preview_of(
            attachment(
                filename="Umsatz.xls",
                content_type="application/vnd.ms-excel",
                size=2048,
                payload=b"\xd0\xcf\x11\xe0",
            )
        )
        assert found.kind == PreviewKind.NONE.value
        assert found.note == OLD_OFFICE
        assert (found.filename, found.size_label) == ("Umsatz.xls", "2.0 KB")

    def test_a_file_nothing_can_draw_says_so_and_still_names_itself(self) -> None:
        """A zip is not an old Office file: nothing about its format is going
        to change, so it keeps the flat sentence."""
        found = preview_of(
            attachment(
                filename="Belege.zip",
                content_type="application/zip",
                size=2048,
                payload=b"PK\x03\x04",
            )
        )
        assert found.kind == PreviewKind.NONE.value
        assert found.note == NOT_PREVIEWABLE
        assert (found.filename, found.size_label) == ("Belege.zip", "2.0 KB")

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


class TestTheTwoFilesThatAreReadRatherThanRendered:
    """A ``.ics`` and a ``.vcf`` reach the panel as records.

    What each reader makes of a file belongs to ``test_ui_message_invite`` and
    ``test_ui_message_vcard``; this asks only what this module owns — that the
    projection routes the file to one, hands the panel the records, and prints
    the file instead when there is nothing to hand over.
    """

    def test_an_invitation_arrives_as_an_event_and_not_as_its_own_lines(self) -> None:
        found = preview_of(
            attachment(
                filename="Termin.ics",
                content_type="text/calendar",
                payload=ICS.encode(),
            )
        )
        assert found.kind == PreviewKind.CALENDAR.value
        assert [one.summary for one in found.events] == ["Sprint Review"]
        assert found.text == ""

    def test_a_contact_card_arrives_as_a_person(self) -> None:
        found = preview_of(
            attachment(
                filename="Anna.vcf", content_type="text/vcard", payload=VCF.encode()
            )
        )
        assert found.kind == PreviewKind.CONTACT.value
        assert [one.name for one in found.contacts] == ["Anna Bauer"]

    def test_a_calendar_with_no_event_in_it_is_printed_instead(self) -> None:
        """The fallback that makes the whole feature safe to have: a file this
        cannot read is still a file the reader can read."""
        text = "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nSUMMARY:Milch\r\nEND:VTODO"
        found = preview_of(
            attachment(
                filename="Liste.ics",
                content_type="text/calendar",
                payload=text.encode(),
            )
        )
        assert found.kind == PreviewKind.TEXT.value
        assert "VTODO" in found.text
        assert found.events == []

    def test_a_vcf_that_is_not_one_is_printed_instead(self) -> None:
        found = preview_of(
            attachment(
                filename="Notiz.vcf",
                content_type="text/vcard",
                payload=b"Sehr geehrte Damen und Herren",
            )
        )
        assert found.kind == PreviewKind.TEXT.value
        assert found.text == "Sehr geehrte Damen und Herren"

    def test_an_exported_calendar_says_that_it_was_cut(self) -> None:
        events = [
            line
            for index in range(EVENT_LIMIT + 2)
            for line in ("BEGIN:VEVENT", f"SUMMARY:{index}", "END:VEVENT")
        ]
        text = "\r\n".join(["BEGIN:VCALENDAR", *events, "END:VCALENDAR"])
        found = preview_of(
            attachment(
                filename="Kalender.ics",
                content_type="text/calendar",
                payload=text.encode(),
            )
        )
        assert len(found.events) == EVENT_LIMIT
        assert found.note == EVENTS_CUT

    def test_an_exported_address_book_says_the_same(self) -> None:
        cards = [
            line
            for index in range(CARD_LIMIT + 2)
            for line in ("BEGIN:VCARD", f"FN:P{index}", "END:VCARD")
        ]
        found = preview_of(
            attachment(
                filename="Adressbuch.vcf",
                content_type="text/vcard",
                payload="\r\n".join(cards).encode(),
            )
        )
        assert len(found.contacts) == CARD_LIMIT
        assert found.note == CARDS_CUT

    def test_a_calendar_that_fits_says_nothing(self) -> None:
        found = preview_of(
            attachment(
                filename="Termin.ics",
                content_type="text/calendar",
                payload=ICS.encode(),
            )
        )
        assert found.note == ""

    def test_a_card_written_in_windows_1252_keeps_its_umlaut(self) -> None:
        """The bug a screenshot found: ``Gesch\ufffdftsleitung`` in the panel,
        because every attachment was decoded as UTF-8 with replacement."""
        text = VCF.replace("FN:Anna Bauer", "FN:Anna Bauer\r\nTITLE:Geschäftsleitung")
        found = preview_of(
            attachment(
                filename="Anna.vcf",
                content_type="text/x-vcard",
                charset="iso-8859-1",
                payload=text.encode("cp1252"),
            )
        )
        assert found.contacts[0].title == "Geschäftsleitung"

    def test_a_card_that_names_no_charset_at_all_keeps_it_too(self) -> None:
        """Which is the common case: the part carried no ``charset``."""
        text = VCF.replace("FN:Anna Bauer", "FN:Anna Bauer\r\nTITLE:Geschäftsleitung")
        found = preview_of(
            attachment(
                filename="Anna.vcf",
                content_type="text/x-vcard",
                payload=text.encode("cp1252"),
            )
        )
        assert found.contacts[0].title == "Geschäftsleitung"

    def test_an_email_row_becomes_something_a_reader_can_press(self) -> None:
        found = preview_of(
            attachment(
                filename="Anna.vcf", content_type="text/vcard", payload=VCF.encode()
            )
        )
        assert found.contacts[0].emails[0].href == "mailto:anna@example.com"


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
        assert base64.b64decode(opened.preview.encoded).startswith(b"%PDF")
        assert not opened.preview_loading

    async def test_a_text_file_comes_back_readable(
        self, opened: OtherDetailState
    ) -> None:
        await opened.show_attachment(0)
        assert opened.preview.kind == PreviewKind.TEXT.value
        assert "Kaffee;9,90" in opened.preview.text

    async def test_a_second_file_does_not_read_the_original_again(
        self, opened: OtherDetailState, published: ArchiveReader, monkeypatch
    ) -> None:
        """Opening two files on one message is one read, not two.

        The parse is the expensive half: ``render_message`` decodes *every*
        attachment's bytes, so re-running it per card decoded both files twice
        to keep one each time.
        """
        reads = 0
        original = published.raw_message

        def counted(digest: str) -> bytes | None:
            nonlocal reads
            reads += 1
            return original(digest)

        monkeypatch.setattr(published, "raw_message", counted)
        await opened.show_attachment(0)
        await opened.show_attachment(1)

        assert reads == 1
        assert opened.preview.filename == "Rechnung.pdf"

    async def test_the_files_are_let_go_when_another_message_opens(
        self, opened: OtherDetailState, digest: str
    ) -> None:
        """The cache lives exactly as long as the message on screen, so a
        reader moving down a list does not accumulate everyone's attachments."""
        await opened.show_attachment(0)
        assert opened._attachments

        await opened._open_message("m2", digest)

        assert opened._attachments == []

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
        assert (opened.preview.source, opened.preview.encoded) == ("", "")

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


class TestTheRecordComponents:
    """That the two record shapes build at all, against any state.

    A component is built once at compile time out of the state class's Vars, so
    the thing that breaks here is a field that does not exist or a ``foreach``
    over something Reflex cannot resolve — and both of those are silent until a
    page is compiled. Each of these walks two levels of ``foreach``, the outer
    over the records and the inner over an event's attendees or a card's
    numbers, which is where a list Reflex cannot type shows up as
    ``Sequence[NoReturn]`` and raises.
    """

    def test_a_calendar_panel_builds(self) -> None:
        """Against a concrete state, because a mixin's fields are not ``Var``s
        until Reflex has copied them into one — the panel would build out of
        the *values* and ``rx.foreach`` would refuse the empty list."""
        assert calendar_preview(OtherDetailState) is not None

    def test_a_contact_panel_builds(self) -> None:
        assert contact_preview(OtherDetailState) is not None

    def test_the_body_draws_every_shape_there_is(self) -> None:
        """The chain in ``_preview_body`` is folded from a table, and a shape
        left out of that table is a shape that silently falls through to
        *No preview*."""
        drawn = str(message_pane(OtherDetailState))
        assert all(one.value in drawn for one in PreviewKind)


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
