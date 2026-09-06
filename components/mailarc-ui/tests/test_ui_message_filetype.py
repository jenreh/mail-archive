"""Tests for :mod:`mailarc_ui.message_detail.filetype`.

One table read by three places — the header card, the preview panel and the
search form's file filter — so this is where the table itself is checked and
the other two suites only check what they *do* with the answer.

Pure functions throughout: a content type and a filename in, a family out. No
archive, no state, no component.
"""

import pytest

from mailarc_core.mail.model import ParsedAttachment
from mailarc_ui.message_detail import (
    EXTENSION_KINDS,
    FILE_ICONS,
    FILE_LABELS,
    TYPE_KINDS,
    AttachmentRow,
    FileKind,
    file_kind,
    type_label,
)
from mailarc_ui.message_detail.filetype import (
    DULL_SUBTYPES,
    GENERIC_TYPES,
    LABEL_LENGTH,
    OOXML_SUFFIXES,
    OOXML_TYPES,
    TYPE_PREFIXES,
    is_ooxml,
)


class TestTheFamilyAFileFallsInto:
    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("application/pdf", FileKind.PDF),
            ("image/png", FileKind.IMAGE),
            ("image/svg+xml", FileKind.IMAGE),
            ("image/heic", FileKind.IMAGE),
            ("audio/mpeg", FileKind.AUDIO),
            ("video/quicktime", FileKind.VIDEO),
            ("text/html", FileKind.WEB),
            ("application/xhtml+xml", FileKind.WEB),
            ("text/plain", FileKind.TEXT),
            ("text/csv", FileKind.TABLE),
            ("text/calendar", FileKind.CALENDAR),
            ("application/ics", FileKind.CALENDAR),
            ("application/json", FileKind.DATA),
            ("message/rfc822", FileKind.MAIL),
            ("application/vnd.ms-excel", FileKind.SPREADSHEET),
            ("application/msword", FileKind.DOCUMENT),
            ("application/vnd.ms-powerpoint", FileKind.SLIDES),
            ("application/zip", FileKind.ARCHIVE),
            ("application/x-something-nobody-has-heard-of", FileKind.OTHER),
        ],
    )
    def test_the_type_decides(self, content_type: str, expected: FileKind) -> None:
        assert file_kind(content_type, "whatever.bin") is expected

    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("text/csv", FileKind.TABLE),
            ("text/calendar", FileKind.CALENDAR),
            ("text/html", FileKind.WEB),
            ("text/rtf", FileKind.DOCUMENT),
            ("text/xml", FileKind.DATA),
            ("text/markdown", FileKind.TEXT),
        ],
    )
    def test_a_named_text_type_beats_the_text_prefix(
        self, content_type: str, expected: FileKind
    ) -> None:
        """``text/`` is the last prefix tried, so ``text/csv`` is a table and
        not "text that happens to have commas in it"."""
        assert file_kind(content_type) is expected

    def test_a_type_is_read_however_the_sender_cased_and_padded_it(self) -> None:
        assert file_kind("  Image/PNG  ") is FileKind.IMAGE

    def test_a_type_still_carrying_its_charset_classifies(self) -> None:
        """A parser hands back a bare type, but a type read back off a graph
        node is whatever was stored — a stray ``; charset=utf-8`` must not
        drop a text file into ``OTHER``."""
        assert file_kind("text/plain; charset=utf-8") is FileKind.TEXT

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("Rechnung.pdf", FileKind.PDF),
            ("Rechnung.PDF", FileKind.PDF),
            ("scan.jpg", FileKind.IMAGE),
            ("Umsatz.xlsx", FileKind.SPREADSHEET),
            ("Anlagen.zip", FileKind.ARCHIVE),
            ("Termin.ics", FileKind.CALENDAR),
            ("weiterleitung.eml", FileKind.MAIL),
            ("noextension", FileKind.OTHER),
            ("Re: Fwd. Notizen", FileKind.OTHER),
            (None, FileKind.OTHER),
        ],
    )
    def test_the_name_answers_when_the_type_says_nothing(
        self, filename: str | None, expected: FileKind
    ) -> None:
        """``application/octet-stream`` is what several mail programs send for
        every attachment; without this a whole archive of invoices is
        unclassified."""
        assert file_kind("application/octet-stream", filename) is expected

    def test_a_stated_type_is_never_overruled_by_a_name(self) -> None:
        """The other way round would let a file *called* ``Rechnung.pdf``
        overrule a sender who knew it was an archive."""
        assert file_kind("application/zip", "Rechnung.pdf") is FileKind.ARCHIVE

    def test_an_empty_type_is_a_shrug_and_not_a_family(self) -> None:
        assert file_kind("", "bild.png") is FileKind.IMAGE


class TestWhatTheCardCallsIt:
    @pytest.mark.parametrize(
        ("content_type", "filename", "expected"),
        [
            ("application/pdf", "Zeitplan.pdf", "PDF"),
            ("application/octet-stream", "Rechnung.PDF", "PDF"),
            ("image/png", "Logo.png", "PNG"),
            ("image/jpeg", "scan.JPEG", "JPEG"),
            ("text/csv", "Posten.csv", "CSV"),
            (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "Umsatz.xlsx",
                "XLSX",
            ),
        ],
    )
    def test_the_extension_is_the_name_a_person_would_say(
        self, content_type: str, filename: str, expected: str
    ) -> None:
        assert type_label(content_type, filename) == expected

    def test_a_nameless_part_falls_back_to_its_subtype(self) -> None:
        assert type_label("image/png") == "PNG"

    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("text/plain", "Text"),
            ("message/rfc822", "Email"),
            ("application/octet-stream", "File"),
            (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "Spreadsheet",
            ),
        ],
    )
    def test_a_subtype_that_is_not_a_name_gives_way_to_the_family(
        self, content_type: str, expected: str
    ) -> None:
        """``PLAIN`` names an encoding and ``vnd.openxmlformats-…`` names
        nothing a strip 240 pixels wide can hold."""
        assert type_label(content_type) == expected

    def test_debris_after_a_dot_is_not_an_extension(self) -> None:
        """``Re: Fwd. Notizen`` has a "suffix" of ``. notizen``; a label of
        ``NOTIZEN`` would be the filename's punctuation showing through."""
        assert type_label("application/octet-stream", "Re: Fwd. Notizen") == "File"

    def test_every_dull_subtype_is_short_enough_to_have_needed_listing(
        self,
    ) -> None:
        """The guard exists only for subtypes that would otherwise pass the
        length test; one longer than the cut is already excluded and its entry
        would be a note nobody can act on."""
        assert all(len(one) <= LABEL_LENGTH for one in DULL_SUBTYPES)


class TestTheTablesAgree:
    def test_every_family_has_a_glyph_and_a_word(self) -> None:
        """Maps keyed by an enum rot in silence — nothing type-checks them and
        no coverage report notices a missing entry, but a family without one is
        a ``KeyError`` on somebody's mail."""
        assert set(FILE_ICONS) == set(FileKind)
        assert set(FILE_LABELS) == set(FileKind)

    def test_the_text_prefix_is_tried_last(self) -> None:
        """The one ordering the table depends on, stated as a test rather than
        left to whoever next adds a prefix."""
        assert TYPE_PREFIXES[-1][0] == "text/"

    def test_no_exact_type_is_also_a_shrug(self) -> None:
        """A type in both tables would classify itself and so never reach the
        filename fallback that being a shrug is supposed to trigger."""
        assert not set(TYPE_KINDS) & GENERIC_TYPES

    def test_every_extension_starts_with_a_dot(self) -> None:
        """``PurePosixPath.suffix`` includes it; an entry without one is an
        entry that can never match."""
        assert all(one.startswith(".") for one in EXTENSION_KINDS)


class TestWhatTheStripShows:
    def test_a_row_carries_its_own_glyph_and_word(self) -> None:
        """Stored on the row, not derived where it is drawn: what reads them
        is markup in the browser, where a Python call cannot run."""
        row = AttachmentRow.from_attachment(
            ParsedAttachment(
                filename="Umsatz.xlsx",
                content_type="application/octet-stream",
                size=2048,
            )
        )
        assert (row.kind_label, row.size_label) == ("XLSX", "2.0 KB")
        assert row.icon == FILE_ICONS[FileKind.SPREADSHEET]

    def test_a_nameless_part_still_gets_a_glyph(self) -> None:
        row = AttachmentRow.from_attachment(
            ParsedAttachment(content_type="application/pdf", size=10)
        )
        assert row.icon == FILE_ICONS[FileKind.PDF]
        assert row.kind_label == "PDF"


class TestWhetherAViewerCanOpenIt:
    """The narrower question :func:`is_ooxml` answers, and why it exists.

    :class:`FileKind` groups by what a file *is*, which is the right grouping
    for a glyph and for the search filter: a ``.key`` and a ``.pptx`` are both
    slides. The viewers group by what a renderer can *parse*, and the three
    that exist read OOXML only. So this is a second, narrower question over the
    same inputs — not a second classifier, because it follows the same rule
    :func:`file_kind` does about types and names.
    """

    @pytest.mark.parametrize(
        "content_type",
        [
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ],
    )
    def test_a_stated_ooxml_type_is_believed_without_a_name(
        self, content_type: str
    ) -> None:
        """A part that says what it is needs no filename to be opened."""
        assert is_ooxml(content_type, None) is True

    @pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx"])
    def test_a_generic_type_falls_back_to_the_extension(self, suffix: str) -> None:
        """The mail programs that send ``octet-stream`` for everything.

        Without this the common case — an Outlook attachment with no stated
        type — would never reach a viewer.
        """
        assert is_ooxml("application/octet-stream", f"Angebot{suffix}") is True

    @pytest.mark.parametrize(
        ("content_type", "filename"),
        [
            ("application/msword", "Vertrag.doc"),
            ("application/rtf", "Notiz.rtf"),
            ("application/vnd.ms-excel", "Umsatz.xls"),
            ("application/vnd.ms-powerpoint", "Pitch.ppt"),
            ("application/vnd.oasis.opendocument.text", "Brief.odt"),
            ("application/vnd.oasis.opendocument.spreadsheet", "Zahlen.ods"),
            ("application/vnd.oasis.opendocument.presentation", "Vortrag.odp"),
            ("application/octet-stream", "Keynote.key"),
            ("application/octet-stream", "Tabelle.numbers"),
            ("application/octet-stream", "Brief.pages"),
        ],
    )
    def test_the_older_and_foreign_office_formats_are_not(
        self, content_type: str, filename: str
    ) -> None:
        """The whole reason the predicate exists.

        Every one of these is a :class:`FileKind` the viewers now claim —
        ``DOCUMENT``, ``SPREADSHEET`` or ``SLIDES`` — and not one of them is a
        zip full of OOXML. Handing a Keynote file to a ``.pptx`` parser is the
        bug this test is here to keep out.
        """
        assert file_kind(content_type, filename) in {
            FileKind.DOCUMENT,
            FileKind.SPREADSHEET,
            FileKind.SLIDES,
        }
        assert is_ooxml(content_type, filename) is False

    def test_a_stated_old_type_beats_a_modern_extension(self) -> None:
        """The tie-break runs the same way round as :func:`file_kind`'s.

        A sender who said ``application/msword`` has said something specific,
        and a ``.docx`` on the end of the name does not override it — the same
        reasoning that stops ``Rechnung.pdf`` overriding a stated zip.
        """
        assert is_ooxml("application/msword", "Vertrag.docx") is False

    def test_a_name_that_is_a_sentence_is_not_an_exception(self) -> None:
        """``PurePosixPath`` on ``Re: Fwd. Notes`` returns ``. notes``."""
        assert is_ooxml("application/octet-stream", "Re: Fwd. Notes") is False

    def test_every_ooxml_type_is_one_the_family_table_knows(self) -> None:
        """The two tables cannot drift apart.

        :data:`OOXML_TYPES` names three content types that :data:`TYPE_KINDS`
        must also map, or a file would be openable by a viewer the panel never
        offers it to.
        """
        assert OOXML_TYPES <= set(TYPE_KINDS)

    def test_every_ooxml_suffix_is_one_the_extension_table_knows(self) -> None:
        """The same guard on the other half of the rule."""
        assert OOXML_SUFFIXES <= set(EXTENSION_KINDS)
