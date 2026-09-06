"""Tests for :mod:`mailarc_ui.message_detail.contentline`.

The grammar under both readers, so the things that go wrong here go wrong for
a meeting *and* for a contact card at the same time. Three of them are worth
the file on their own: unfolding, because a description that is not unfolded
loses every space in it; the quote-aware split, because a name with a comma in
it is common and takes half a parameter with it when the split is naive; and
the single-pass unescaping, because the obvious four-replacement version turns
a Windows path into a line break.

No archive and no browser — text in, records out.
"""

import pytest

from mailarc_ui.message_detail.contentline import (
    ContentLine,
    blocks,
    components,
    every,
    first,
    outer,
    read_line,
    read_lines,
    text_of,
    unescape,
    unfold,
)


class TestUnfolding:
    """A long line split at 75 octets and continued with one space."""

    def test_a_continuation_line_rejoins_the_one_above_it(self) -> None:
        assert unfold("SUMMARY:Sprint\r\n  Review") == ["SUMMARY:Sprint Review"]

    def test_the_one_leading_space_goes_and_the_rest_stays(self) -> None:
        """Exactly one whitespace character is the fold; a second is content —
        which is what keeps an indented line of an agenda indented."""
        assert unfold("X:a\n   b") == ["X:a  b"]

    def test_a_tab_folds_too(self) -> None:
        assert unfold("X:a\n\tb") == ["X:ab"]

    def test_bare_newlines_are_read_the_same_as_crlf(self) -> None:
        """The standards require CRLF and mail carries plenty of LF."""
        assert unfold("A:1\nB:2") == unfold("A:1\r\nB:2") == ["A:1", "B:2"]

    def test_a_file_that_opens_with_a_fold_does_not_lose_it(self) -> None:
        """Nonsense input, and the guard that stops it being an ``IndexError``
        on the empty list of lines so far."""
        assert unfold(" orphan") == [" orphan"]


class TestReadingOneLine:
    def test_a_bare_property_is_a_name_and_a_value(self) -> None:
        found = read_line("SUMMARY:Standup")
        assert found is not None
        assert (found.name, found.value, found.params) == ("SUMMARY", "Standup", {})

    def test_the_name_is_upper_cased_because_the_standard_says_it_may_be_any_case(
        self,
    ) -> None:
        found = read_line("dtstart;tzid=Europe/Berlin:20260906T140000")
        assert found is not None
        assert found.name == "DTSTART"
        assert found.param("TZID") == "Europe/Berlin"

    def test_a_quoted_parameter_may_hold_the_separators(self) -> None:
        """The reason the split is quote-aware at all: a display name with a
        comma in it is the ordinary way of writing a surname first."""
        found = read_line('ATTENDEE;CN="Bauer, Anna";ROLE=CHAIR:mailto:a@example.com')
        assert found is not None
        assert found.param("CN") == "Bauer, Anna"
        assert found.value == "mailto:a@example.com"

    def test_a_quoted_parameter_may_hold_a_colon(self) -> None:
        """Exchange writes ``ALTREP="cid:…"`` and a naive split on the first
        colon would take the value from inside it."""
        found = read_line('DESCRIPTION;ALTREP="cid:part1@host":Agenda')
        assert found is not None
        assert found.value == "Agenda"

    def test_a_line_with_no_colon_is_not_a_line(self) -> None:
        assert read_line("BEGIN") is None

    def test_a_blank_line_is_not_a_line(self) -> None:
        assert read_line("") is None
        assert read_line("   ") is None

    def test_a_line_with_no_name_in_front_of_the_colon_is_not_a_line(self) -> None:
        """Which is what the middle of a folded photograph looks like to a
        reader that has lost its place."""
        assert read_line(":value") is None

    def test_a_bare_parameter_is_recorded_without_being_named(self) -> None:
        """vCard 2.1 wrote ``TEL;CELL;VOICE:+49…``, and it is still what
        Outlook exports. This module has no idea what a bare ``CELL`` is a
        parameter *of*, so it keeps the word and lets the vCard reader decide.
        """
        found = read_line("TEL;CELL;VOICE:+49 170 1")
        assert found is not None
        assert (found.name, found.value, found.params) == ("TEL", "+49 170 1", {})
        assert found.flags == ("CELL", "VOICE")

    def test_a_named_parameter_is_not_also_a_flag(self) -> None:
        found = read_line("TEL;TYPE=CELL:+49 170 1")
        assert found is not None
        assert (found.param("TYPE"), found.flags) == ("CELL", ())

    def test_a_missing_parameter_reads_as_empty_rather_than_raising(self) -> None:
        assert ContentLine(name="X", value="y").param("TZID") == ""


class TestUnescaping:
    @pytest.mark.parametrize(
        ("written", "meant"),
        [
            (r"Agenda:\nDemo", "Agenda:\nDemo"),
            (r"Agenda:\NDemo", "Agenda:\nDemo"),
            (r"Raum 3.14\, Hauptbau", "Raum 3.14, Hauptbau"),
            (r"a\;b", "a;b"),
            (r"C:\\Temp", "C:\\Temp"),
            ("nothing to do", "nothing to do"),
        ],
    )
    def test_the_five_escapes_and_nothing_else(self, written: str, meant: str) -> None:
        assert unescape(written) == meant

    def test_an_escaped_backslash_before_an_n_stays_a_backslash_and_an_n(self) -> None:
        """The whole reason this is one pass. Any pair of sequential
        replacements turns ``\\\\n`` into a line break, and a sender who wrote
        a Windows path gets one in the middle of it."""
        assert unescape(r"C:\\new") == r"C:\new"

    def test_a_backslash_that_escapes_nothing_keeps_the_character(self) -> None:
        assert unescape(r"50\% sicher") == "50% sicher"

    def test_a_trailing_backslash_is_kept_rather_than_read_past_the_end(self) -> None:
        assert unescape("ends with\\") == "ends with\\"


class TestStructuredValues:
    def test_a_structured_value_splits_into_its_fields(self) -> None:
        assert components("Bauer;Anna;;Dr.;") == ["Bauer", "Anna", "", "Dr.", ""]

    def test_an_escaped_separator_is_not_one(self) -> None:
        """The split has to happen before the unescaping, or a semicolon
        written ``\\;`` precisely so it would not separate does."""
        assert components(r"Beispiel GmbH\; Berlin;Vertrieb") == [
            "Beispiel GmbH; Berlin",
            "Vertrieb",
        ]

    def test_the_fields_come_back_unescaped(self) -> None:
        assert components(r"a\,b;c") == ["a,b", "c"]

    def test_one_field_is_still_a_list(self) -> None:
        assert components("Beispiel GmbH") == ["Beispiel GmbH"]


class TestBlocks:
    CALENDAR = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "METHOD:REQUEST",
            "BEGIN:VTIMEZONE",
            "TZID:Europe/Berlin",
            "BEGIN:STANDARD",
            "DTSTART:16011028T030000",
            "END:STANDARD",
            "END:VTIMEZONE",
            "BEGIN:VEVENT",
            "SUMMARY:Standup",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "SUMMARY:Reminder",
            "END:VALARM",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "SUMMARY:Retro",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )

    def test_each_block_comes_back_separately(self) -> None:
        found = blocks(read_lines(self.CALENDAR), "VEVENT")
        assert [text_of(one, "SUMMARY") for one in found] == ["Standup", "Retro"]

    def test_a_nested_block_keeps_its_own_properties(self) -> None:
        """The reason this walks a stack. A ``VALARM`` has a ``SUMMARY`` too,
        and a reader that collected by name would put the reminder's text where
        the meeting's belongs."""
        first_event = blocks(read_lines(self.CALENDAR), "VEVENT")[0]
        assert text_of(first_event, "SUMMARY") == "Standup"
        assert [one.name for one in first_event] == ["SUMMARY"]

    def test_a_sibling_block_is_skipped_entirely(self) -> None:
        """A ``VTIMEZONE`` carries a ``DTSTART`` of its own — the rule that
        keeps 1601 out of a meeting."""
        assert blocks(read_lines(self.CALENDAR), "VEVENT")[0] == [
            ContentLine(name="SUMMARY", value="Standup")
        ]

    def test_a_block_that_never_ends_keeps_what_it_had(self) -> None:
        """Which is the useful reading of a download that stopped early."""
        text = "BEGIN:VCARD\r\nFN:Anna\r\nEMAIL:a@example.com"
        assert len(blocks(read_lines(text), "VCARD")[0]) == 2

    def test_a_file_with_no_such_block_comes_back_empty(self) -> None:
        assert blocks(read_lines("BEGIN:VTODO\r\nEND:VTODO"), "VEVENT") == []

    def test_a_stray_end_does_not_take_the_reader_with_it(self) -> None:
        assert blocks(read_lines("END:VEVENT\r\nBEGIN:VEVENT\r\nX:1"), "VEVENT") != []


class TestOuterProperties:
    def test_the_calendar_s_own_property_is_found(self) -> None:
        assert outer(read_lines(TestBlocks.CALENDAR), "METHOD") == "REQUEST"

    def test_a_property_of_the_same_name_inside_a_block_is_not(self) -> None:
        """``METHOD`` belongs to the calendar; a ``VEVENT`` may carry anything."""
        text = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nMETHOD:NONSENSE\r\nEND:VEVENT"
        assert outer(read_lines(text), "METHOD") == ""

    def test_a_file_with_no_wrapper_still_has_its_top_level(self) -> None:
        assert outer(read_lines("METHOD:CANCEL"), "METHOD") == "CANCEL"


class TestFindingProperties:
    LINES = read_lines("A:1\r\nB:2\r\nA:3")

    def test_first_takes_the_first(self) -> None:
        found = first(self.LINES, "A")
        assert found is not None
        assert found.value == "1"

    def test_first_of_something_absent_is_none(self) -> None:
        assert first(self.LINES, "Z") is None

    def test_every_keeps_the_order_the_file_wrote(self) -> None:
        assert [one.value for one in every(self.LINES, "A")] == ["1", "3"]

    def test_text_of_something_absent_is_empty_rather_than_none(self) -> None:
        assert text_of(self.LINES, "Z") == ""
