"""Tests for :mod:`mailarc_ui.search.files` — the form's file filter.

Two halves that have to be each other's inverse: the archive's catalogue
becomes the families a picker offers, and the families somebody picked become
that catalogue's entries again. The catalogue has two lists — the types
senders stated, and the *names* of the files they stated nothing about — and
both directions have to carry both, or a person picking **PDF** misses every
invoice that arrived as ``application/octet-stream``.

Everything here is pure, so the join is checkable without a graph; that the
resulting filter actually narrows a real archive is
``test_archive_search_local``'s claim.
"""

import pytest

from mailarc_ui.message_detail import FILE_LABELS, FileKind
from mailarc_ui.search.files import (
    KIND_LIMIT,
    families_of,
    kind_options,
    names_of,
    offered_kinds,
    types_of,
)

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
ZIP = "application/zip"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

HELD = (PDF, PNG, JPEG, ZIP, XLSX)
"""The types one archive turned out to hold, commonest first."""

NAMED = ("Rechnung.pdf", "Archiv.zip", ".env")
"""Files from the same archive whose sender stated no type at all.

Two of them are a family the types above already offer and one is a family
nothing else could name — which is the whole reason the second list exists.
"""


class TestWhatThePickerOffers:
    def test_a_family_appears_once_however_many_types_feed_it(self) -> None:
        """Two picture types are one *Image* option; a picker that offered it
        twice would be a picker nobody trusts."""
        assert families_of(HELD).count(FileKind.IMAGE.value) == 1

    def test_the_archive_s_own_order_is_kept(self) -> None:
        """It hands its types back commonest first, so the family a person is
        most likely to want is the one at the top of the list."""
        assert families_of(HELD) == [
            FileKind.PDF.value,
            FileKind.IMAGE.value,
            FileKind.ARCHIVE.value,
            FileKind.SPREADSHEET.value,
        ]

    def test_nothing_held_offers_nothing(self) -> None:
        """What lets the form leave the field out entirely rather than show an
        empty dropdown."""
        assert families_of(()) == []
        assert kind_options(()) == []

    def test_an_option_is_a_family_under_its_own_word(self) -> None:
        assert kind_options((PDF, XLSX)) == [
            {"value": FileKind.PDF.value, "label": FILE_LABELS[FileKind.PDF]},
            {
                "value": FileKind.SPREADSHEET.value,
                "label": FILE_LABELS[FileKind.SPREADSHEET],
            },
        ]

    def test_a_file_nobody_typed_is_offered_by_its_name(self) -> None:
        """The claim the second list is for. ``Rechnung.pdf`` arrived as
        ``application/octet-stream``; classified by that type it would be one
        more *File*, and the card on the message calls it *PDF*."""
        assert kind_options((), ("Rechnung.pdf",)) == [
            {"value": FileKind.PDF.value, "label": FILE_LABELS[FileKind.PDF]}
        ]

    def test_a_name_nobody_can_read_either_is_offered_as_a_file(self) -> None:
        """``.env`` has no extension to read, and *File* is a real answer with
        its own word — an archive that holds one should be able to ask for
        it."""
        assert kind_options((), (".env",)) == [
            {"value": FileKind.OTHER.value, "label": FILE_LABELS[FileKind.OTHER]}
        ]

    def test_a_family_both_lists_hold_is_still_one_option(self) -> None:
        """The stated PDFs and the shrugged ones are one *PDF* option."""
        assert families_of(HELD, NAMED).count(FileKind.PDF.value) == 1

    def test_the_stated_types_come_first(self) -> None:
        """Two lists, each already commonest-first, and no count to interleave
        them by: a sender who named the type is the stronger evidence, so that
        list leads and the names add only what it did not already offer."""
        assert families_of((PDF,), (".env",)) == [
            FileKind.PDF.value,
            FileKind.OTHER.value,
        ]

    def test_the_list_is_bounded(self) -> None:
        assert (
            len(families_of(tuple(f"x/{n}" for n in range(200)) + HELD)) <= KIND_LIMIT
        )


class TestWhatAPickMeans:
    def test_a_family_expands_to_every_type_the_archive_holds_of_it(self) -> None:
        assert types_of([FileKind.IMAGE.value], HELD) == (PNG, JPEG)

    def test_several_families_expand_together(self) -> None:
        assert types_of([FileKind.PDF.value, FileKind.ARCHIVE.value], HELD) == (
            PDF,
            ZIP,
        )

    def test_an_empty_pick_is_no_filter_at_all(self) -> None:
        """Not "every type" — an unset filter and a filter naming everything
        are the same result and different statements, and the cheap one is the
        one that emits no clause."""
        assert types_of([], HELD) == ()
        assert types_of(["", "  "], HELD) == ()

    def test_a_family_the_archive_holds_nothing_of_expands_to_nothing(self) -> None:
        """Which would return an empty list — and is exactly what
        :func:`offered_kinds` exists to keep a reader from asking."""
        assert types_of([FileKind.AUDIO.value], HELD) == ()

    def test_a_family_expands_to_the_names_the_archive_holds_of_it(self) -> None:
        """The other half of the expansion: whole names, because that is what
        the archive stores and what it can compare without a taxonomy."""
        assert names_of([FileKind.PDF.value], NAMED) == ("Rechnung.pdf",)
        assert names_of([FileKind.OTHER.value], NAMED) == (".env",)

    def test_an_empty_pick_names_nothing_either(self) -> None:
        assert names_of([], NAMED) == ()
        assert names_of(["", "  "], NAMED) == ()

    def test_one_family_expands_into_both_halves_at_once(self) -> None:
        """A picked *Archive* is the stated zip **and** the shrugged one; the
        filter ORs them, so the message carrying either comes back."""
        picked = [FileKind.ARCHIVE.value]

        assert types_of(picked, HELD) == (ZIP,)
        assert names_of(picked, NAMED) == ("Archiv.zip",)

    def test_the_two_halves_are_each_other_s_inverse(self) -> None:
        """Every family the picker offers expands back to at least one type or
        one name, so no option on the form can return an empty list."""
        for option in kind_options(HELD, NAMED):
            picked = [option["value"]]
            assert types_of(picked, HELD) or names_of(picked, NAMED), option


class TestGuardingWhatComesBack:
    @pytest.mark.parametrize(
        ("picked", "expected"),
        [
            ([FileKind.PDF.value], [FileKind.PDF.value]),
            ([FileKind.AUDIO.value], []),
            (["nonsense"], []),
            ([""], []),
            (
                [FileKind.PDF.value, FileKind.PDF.value],
                [FileKind.PDF.value],
            ),
        ],
    )
    def test_only_what_was_offered_survives(
        self, picked: list[str], expected: list[str]
    ) -> None:
        """The value arrives over the socket. A family this archive holds no
        files of would answer with an empty list, which reads like an empty
        archive rather than like a bad filter."""
        assert offered_kinds(picked, kind_options(HELD, NAMED)) == expected

    def test_the_order_a_person_picked_in_is_kept(self) -> None:
        picked = [FileKind.ARCHIVE.value, FileKind.PDF.value]

        assert offered_kinds(picked, kind_options(HELD)) == picked

    def test_nothing_offered_keeps_nothing(self) -> None:
        assert offered_kinds([FileKind.PDF.value], []) == []
