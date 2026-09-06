"""Tests for :mod:`mailarc_ui.message_detail.vcard`.

A ``.vcf`` in, a contact card out. Two of the classes below are about safety
rather than about reading: a ``PHOTO`` may be a URL at somebody else's server
and a ``URL`` may be ``javascript:``, and both of those come out of a file a
stranger wrote and go into an element that would honour them.

The rest is the ordinary work — a name in five fields, an address in seven, and
the two versions of vCard that spell a photograph differently.
"""

import base64

import pytest

from mailarc_ui.message_detail.contentline import ContentLine, read_lines
from mailarc_ui.message_detail.vcard import (
    CARD_LIMIT,
    DETAIL_LIMIT,
    NO_NAME,
    PHOTO_LIMIT,
    ContactCard,
    address_label,
    birthday_label,
    display_name,
    link_href,
    mail_href,
    read_contacts,
    type_label,
)

PNG = base64.b64encode(bytes.fromhex("89504e470d0a1a0a")).decode()
"""A real PNG signature, base64 — enough to be decodable, small enough to read."""


def vcf(*lines: str, version: str = "3.0") -> str:
    return "\r\n".join(["BEGIN:VCARD", f"VERSION:{version}", *lines, "END:VCARD"])


def one(*lines: str, version: str = "3.0") -> ContactCard:
    return read_contacts(vcf(*lines, version=version)).cards[0]


class TestWhatTheCardIsCalled:
    def test_the_formatted_name_is_what_the_person_wrote(self) -> None:
        assert one("FN:Dr. Anna Bauer", "N:Bauer;Anna;;Dr.;").name == "Dr. Anna Bauer"

    def test_a_card_with_only_the_structured_name_is_assembled(self) -> None:
        """Not in the file's own order: ``Bauer;Anna;Maria;Dr.;`` is read out
        as somebody would say it."""
        assert one("N:Bauer;Anna;Maria;Dr.;").name == "Dr. Anna Maria Bauer"

    def test_missing_fields_leave_no_gaps(self) -> None:
        assert one("N:Bauer;Anna;;;").name == "Anna Bauer"

    def test_a_suffix_comes_last(self) -> None:
        assert one("N:Bauer;Anna;;;M.A.").name == "Anna Bauer M.A."

    def test_a_card_with_no_name_at_all_is_still_called_something(self) -> None:
        assert one("EMAIL:a@example.com").name == NO_NAME

    def test_an_escaped_semicolon_in_a_name_is_not_a_field_separator(self) -> None:
        assert display_name(read_lines(r"N:Bauer\; Anna;;;;")) == "Bauer; Anna"


class TestTheDetails:
    CARD = (
        "FN:Anna Bauer",
        "ORG:Beispiel GmbH;Vertrieb;Nord",
        "TITLE:Leiterin",
        "EMAIL;TYPE=INTERNET,WORK:anna@example.com",
        "EMAIL;TYPE=HOME:anna@privat.de",
        "TEL;TYPE=WORK,VOICE:+49 30 1",
        "TEL;TYPE=CELL:+49 170 1",
        "ADR;TYPE=WORK:;;Musterstr. 1;Berlin;;10115;Deutschland",
        "URL:https://example.com",
        "BDAY:1985-03-17",
        r"NOTE:Ruft gern zurück\, meistens.",
    )

    def test_an_organisation_and_its_units_are_one_line(self) -> None:
        assert one(*self.CARD).organisation == "Beispiel GmbH · Vertrieb · Nord"

    def test_the_role_is_kept(self) -> None:
        assert one(*self.CARD).title == "Leiterin"

    def test_a_role_property_stands_in_for_a_missing_title(self) -> None:
        assert one("FN:A", "ROLE:Hausmeister").title == "Hausmeister"

    def test_every_email_address_is_listed_with_its_kind(self) -> None:
        found = one(*self.CARD)
        assert [(row.label, row.value) for row in found.emails] == [
            ("Work", "anna@example.com"),
            ("Home", "anna@privat.de"),
        ]

    def test_every_number_is_listed_with_its_kind(self) -> None:
        found = one(*self.CARD)
        assert [(row.label, row.value) for row in found.phones] == [
            ("Work", "+49 30 1"),
            ("Mobile", "+49 170 1"),
        ]

    def test_an_address_reads_as_something_a_letter_could_go_to(self) -> None:
        assert one(*self.CARD).addresses[0].value == (
            "Musterstr. 1, 10115 Berlin, Deutschland"
        )

    def test_a_birthday_is_shown_the_way_a_date_is_shown(self) -> None:
        assert one(*self.CARD).birthday == "17.03.1985"

    def test_a_note_keeps_its_escapes_resolved(self) -> None:
        assert one(*self.CARD).note == "Ruft gern zurück, meistens."

    def test_a_card_with_nothing_on_it_has_empty_lists_and_not_nones(self) -> None:
        found = one("FN:Anna Bauer")
        assert (found.emails, found.phones, found.addresses, found.links) == (
            [],
            [],
            [],
            [],
        )

    def test_a_merged_record_stops_listing_at_the_limit(self) -> None:
        numbers = [f"TEL:+49 30 {index}" for index in range(DETAIL_LIMIT + 5)]
        assert len(one("FN:A", *numbers).phones) == DETAIL_LIMIT

    def test_an_empty_value_is_not_a_row(self) -> None:
        found = one("FN:A", "EMAIL:", "EMAIL:a@example.com")
        assert [row.value for row in found.emails] == ["a@example.com"]


class TestTypeWords:
    @pytest.mark.parametrize(
        ("stated", "said"),
        [
            ("WORK", "Work"),
            ("CELL", "Mobile"),
            ("work", "Work"),
            ("WORK,VOICE", "Work"),
            ("VOICE,FAX,WORK", "Work Fax"),
            ("INTERNET", ""),
            ("PREF", ""),
            ("", ""),
            ("SOMETHING-ELSE", ""),
        ],
    )
    def test_only_the_words_that_tell_a_reader_something(
        self, stated: str, said: str
    ) -> None:
        """``VOICE``, ``INTERNET`` and ``PREF`` are true of nearly every line
        and so distinguish nothing."""
        assert (
            type_label(ContentLine(name="TEL", value="x", params={"TYPE": stated}))
            == said
        )

    def test_the_same_word_twice_is_said_once(self) -> None:
        prop = ContentLine(name="TEL", value="x", params={"TYPE": "CELL,MOBILE"})
        assert type_label(prop) == "Mobile"

    @pytest.mark.parametrize(
        ("flags", "said"),
        [
            (("WORK", "VOICE"), "Work"),
            (("CELL",), "Mobile"),
            (("FAX", "WORK"), "Work Fax"),
            (("VOICE",), ""),
            ((), ""),
        ],
    )
    def test_vcard_two_one_wrote_the_type_as_a_bare_word(
        self, flags: tuple[str, ...], said: str
    ) -> None:
        """``TEL;WORK;VOICE:+49…`` is what Outlook exports and what the cards
        in this archive are actually written in — a card whose kinds were
        dropped shows four numbers under each other with nothing to tell them
        apart."""
        assert type_label(ContentLine(name="TEL", value="x", flags=flags)) == said

    def test_a_real_vcard_two_one_card_keeps_its_kinds(self) -> None:
        found = one(
            "FN:Klaus Richter",
            "TEL;WORK:05241 /21194-12",
            "TEL;CELL:0173 /5125532",
            "TEL;FAX;WORK:05241 /21194-20",
            version="2.1",
        )
        assert [row.label for row in found.phones] == ["Work", "Mobile", "Work Fax"]


class TestAddresses:
    def test_the_postcode_joins_the_town_rather_than_becoming_a_field(self) -> None:
        prop = ContentLine(name="ADR", value=";;Hauptstr. 2;Köln;NRW;50667;DE")
        assert address_label(prop) == "Hauptstr. 2, 50667 Köln, NRW, DE"

    def test_a_post_office_box_stands_in_for_a_missing_street(self) -> None:
        prop = ContentLine(name="ADR", value="Postfach 12;;;Berlin;;10115;")
        assert address_label(prop) == "Postfach 12, 10115 Berlin"

    def test_an_address_that_is_all_empty_fields_is_not_a_row(self) -> None:
        assert one("FN:A", "ADR:;;;;;;").addresses == []

    def test_a_short_address_does_not_fall_off_the_end(self) -> None:
        assert address_label(ContentLine(name="ADR", value=";;Weg 1")) == "Weg 1"


class TestBirthdays:
    @pytest.mark.parametrize(
        ("written", "said"),
        [
            ("19850317", "17.03.1985"),
            ("1985-03-17", "17.03.1985"),
            ("--0317", "--0317"),
            ("1985-03-17T00:00:00Z", "1985-03-17T00:00:00Z"),
            ("", ""),
        ],
    )
    def test_a_date_is_formatted_and_anything_else_is_shown_as_written(
        self, written: str, said: str
    ) -> None:
        """A birthday with the year withheld is legal in vCard 4.0 and is not a
        date; showing it as the file wrote it beats a blank or a wrong day."""
        assert birthday_label(written) == said


class TestPhotographs:
    def test_a_vcard_three_photo_is_wrapped_in_a_data_uri(self) -> None:
        found = one("FN:A", f"PHOTO;ENCODING=b;TYPE=PNG:{PNG}")
        assert found.photo == f"data:image/png;base64,{PNG}"

    def test_the_other_spelling_of_the_encoding_works_too(self) -> None:
        found = one("FN:A", f"PHOTO;ENCODING=BASE64;TYPE=JPEG:{PNG}")
        assert found.photo.startswith("data:image/jpeg;base64,")

    def test_a_photo_with_no_stated_format_is_still_shown(self) -> None:
        assert one("FN:A", f"PHOTO;ENCODING=b:{PNG}").photo.startswith("data:image/")

    def test_vcard_two_one_named_the_format_as_a_bare_word_too(self) -> None:
        found = one("FN:A", f"PHOTO;PNG;ENCODING=BASE64:{PNG}", version="2.1")
        assert found.photo.startswith("data:image/png;base64,")

    def test_a_full_media_type_in_the_parameter_is_used_as_given(self) -> None:
        found = one("FN:A", f"PHOTO;ENCODING=b;TYPE=image/webp:{PNG}")
        assert found.photo.startswith("data:image/webp;base64,")

    def test_a_vcard_four_photo_already_carries_its_own_uri(self) -> None:
        uri = f"data:image/png;base64,{PNG}"
        assert one("FN:A", f"PHOTO:{uri}", version="4.0").photo == uri

    def test_a_folded_photo_is_put_back_together_before_it_is_decoded(self) -> None:
        """Which is how every real card carries one — a photograph is thousands
        of characters and the standard folds at 75."""
        text = "\r\n".join(
            [
                "BEGIN:VCARD",
                "FN:A",
                f"PHOTO;ENCODING=b;TYPE=PNG:{PNG[:4]}",
                f" {PNG[4:]}",
                "END:VCARD",
            ]
        )
        assert read_contacts(text).cards[0].photo == f"data:image/png;base64,{PNG}"

    def test_a_logo_stands_in_when_there_is_no_photo(self) -> None:
        assert one("FN:A", f"LOGO;ENCODING=b;TYPE=PNG:{PNG}").photo != ""

    def test_a_card_with_no_picture_has_no_picture(self) -> None:
        assert one("FN:A").photo == ""


class TestPhotographsThatWouldFetch:
    """The half of the photo rule that is about not being tracked.

    Opening an archived contact card must not make a request to anybody's
    server — the tracking pixel the mail body's sandbox exists to block, with a
    face on it.
    """

    def test_a_photo_that_is_an_https_url_is_dropped(self) -> None:
        assert (
            one("FN:A", "PHOTO;VALUE=URI:https://tracker.example.com/p.jpg").photo == ""
        )

    def test_a_photo_that_is_a_bare_url_is_dropped(self) -> None:
        assert one("FN:A", "PHOTO:http://tracker.example.com/p.jpg").photo == ""

    def test_a_data_uri_that_is_not_an_image_is_dropped(self) -> None:
        assert one("FN:A", "PHOTO:data:text/html;base64,PHNjcmlwdD4=").photo == ""

    def test_a_photo_that_does_not_decode_is_dropped(self) -> None:
        """Half a picture is not one, and it is the property large enough to be
        cut in half by the text limit."""
        assert one("FN:A", "PHOTO;ENCODING=b;TYPE=PNG:not base64 at all!!").photo == ""

    def test_an_absurd_photo_is_dropped_rather_than_put_on_the_socket(self) -> None:
        huge = "A" * (PHOTO_LIMIT + 4)
        assert one("FN:A", f"PHOTO;ENCODING=b;TYPE=PNG:{huge}").photo == ""


class TestLinksAndAddressesToWriteTo:
    """Whether a value may become an ``href`` — decided here, not in a component."""

    def test_an_email_address_becomes_a_mailto(self) -> None:
        found = one("FN:A", "EMAIL:anna@example.com")
        assert found.emails[0].href == "mailto:anna@example.com"
        assert found.emails[0].external is False

    def test_a_card_that_already_wrote_the_scheme_does_not_get_two(self) -> None:
        assert mail_href("mailto:anna@example.com") == "mailto:anna@example.com"

    def test_something_that_is_not_an_address_gets_no_link(self) -> None:
        assert mail_href("keine Ahnung") == ""
        assert mail_href("") == ""

    def test_a_web_address_is_a_link_that_leaves_the_application(self) -> None:
        found = one("FN:A", "URL:https://example.com")
        assert (found.links[0].href, found.links[0].external) == (
            "https://example.com",
            True,
        )

    def test_a_bare_domain_is_given_https(self) -> None:
        """Which is how a signature block writes one."""
        assert link_href("www.knocke-richter.de") == "https://www.knocke-richter.de"

    @pytest.mark.parametrize(
        "hostile",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "file:///etc/passwd",
            "vbscript:msgbox(1)",
        ],
    )
    def test_a_scheme_that_is_not_the_web_never_reaches_an_href(
        self, hostile: str
    ) -> None:
        """An allow-list, because an anchor honours more schemes than a reader
        would ever expect it to and the value came out of somebody else's file.
        """
        assert link_href(hostile) == ""

    def test_the_value_is_still_shown_even_when_it_is_not_a_link(self) -> None:
        """Refusing to link it is not refusing to say what the file contained."""
        found = one("FN:A", "URL:javascript:alert(1)")
        assert (found.links[0].value, found.links[0].href) == (
            "javascript:alert(1)",
            "",
        )

    def test_a_value_with_a_space_in_it_is_not_a_link(self) -> None:
        assert link_href("https://example.com /../x") == ""

    def test_a_telephone_number_is_deliberately_not_a_link(self) -> None:
        assert one("FN:A", "TEL:+49 30 1").phones[0].href == ""

    def test_nothing_is_not_a_link(self) -> None:
        assert link_href("") == ""
        assert link_href("keine-tld") == ""


class TestWhatTheFileHolds:
    def test_several_cards_all_come_back(self) -> None:
        text = vcf("FN:Anna") + "\r\n" + vcf("FN:Bob")
        assert [card.name for card in read_contacts(text).cards] == ["Anna", "Bob"]

    def test_an_exported_address_book_is_cut_and_the_total_says_how_many(self) -> None:
        text = "\r\n".join(vcf(f"FN:P{index}") for index in range(CARD_LIMIT + 4))
        found = read_contacts(text)
        assert (len(found.cards), found.total) == (CARD_LIMIT, CARD_LIMIT + 4)

    def test_a_file_with_no_card_in_it_holds_nothing(self) -> None:
        assert read_contacts("BEGIN:VCALENDAR\r\nEND:VCALENDAR").cards == []

    def test_nonsense_is_not_an_exception(self) -> None:
        assert read_contacts("Sehr geehrte Damen und Herren").cards == []
        assert read_contacts("").cards == []


class TestTheEncodingsRealCardsArriveIn:
    """The bug that made this worth a test class of its own.

    A German mail program wrote ``Geschäftsleitung`` in Windows-1252 and the
    panel printed ``Gesch�ftsleitung``. What fixed it lives in
    :func:`~mailarc_ui.message_detail.model.decode_raw`; what is checked here
    is that the reader is happy with whatever that hands it, in each of the
    three ways a card arrives.
    """

    CARD = vcf("FN:Klaus Richter", "TITLE:Geschäftsleitung")

    @pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "iso-8859-1"])
    def test_an_umlaut_survives_every_encoding_a_card_arrives_in(
        self, encoding: str
    ) -> None:
        from mailarc_ui.message_detail.model import decode_raw

        text, _ = decode_raw(self.CARD.encode(encoding), 10_000, encoding)
        assert read_contacts(text).cards[0].title == "Geschäftsleitung"

    def test_a_card_that_lies_about_its_encoding_is_still_read(self) -> None:
        """UTF-8 is tried before the label for exactly this: a file that says
        ``iso-8859-1`` and is not would otherwise decode into mojibake without
        anything failing."""
        from mailarc_ui.message_detail.model import decode_raw

        text, _ = decode_raw(self.CARD.encode("utf-8"), 10_000, "iso-8859-1")
        assert read_contacts(text).cards[0].title == "Geschäftsleitung"
