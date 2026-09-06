"""Where the feature is actually decided: words in a question against an archive.

Everything here runs without a model and without a graph, which is the point of
:mod:`mailarc_analytics.nlq.resolve` being pure. The three questions this
feature was built for are German, and all three are below by name — a mention
that is a display name rather than an address, a three-letter company inside a
longer domain, and a "wann war die letzte" that has to be answered by ordering
rather than by ranking.

Two rules are asserted more than once on purpose, because both are ways for a
search to lie rather than to fail:

**Nothing is invented.** ``SearchFilters.senders`` is matched exactly, so an
address that is not in the directory would filter to zero and read as an empty
archive rather than as a bad guess.

**Nothing disappears.** A mention nothing matches goes back into the text and
is reported. Dropped, it would turn "everything from my property manager" into
"everything" — the whole archive, returned confidently.
"""

from datetime import date

import pytest

from mailarc_analytics.nlq.model import (
    AccountOption,
    DirectoryEntry,
    FamilyOption,
    Order,
    Period,
    QueryIntent,
    Vocabulary,
)
from mailarc_analytics.nlq.resolve import (
    EARLIEST,
    resolve,
    resolve_account,
    resolve_addresses,
    resolve_families,
    window_of,
)

TODAY = date(2026, 9, 6)
"""A Sunday in the third quarter, so every period below has a fixed answer."""

DIRECTORY = (
    DirectoryEntry(
        address="no-reply@amazonaws.com",
        names=("AWS Billing",),
        domain="amazonaws.com",
    ),
    DirectoryEntry(
        address="billing@aws.amazon.com",
        names=("Amazon Web Services",),
        domain="aws.amazon.com",
    ),
    DirectoryEntry(
        address="info@santak-immobilien.de",
        names=("Santak Hausverwaltung GmbH",),
        domain="santak-immobilien.de",
    ),
    DirectoryEntry(
        address="service@telekom.de",
        names=("Telekom Kundenservice",),
        domain="telekom.de",
    ),
    DirectoryEntry(
        address="anna@lawsociety.org",
        names=("Anna Müller",),
        domain="lawsociety.org",
    ),
)
"""A small archive with the two traps in it.

``lawsociety.org`` contains the letters of *AWS* and must not answer to it —
that is what the word-boundary tier is for. ``Anna Müller`` is spelled with an
umlaut and has to be findable by somebody who types one, or by somebody who
does not.
"""

FAMILIES = (
    FamilyOption(value="pdf", label="PDF"),
    FamilyOption(value="image", label="Images"),
    FamilyOption(value="spreadsheet", label="Spreadsheets"),
)

ACCOUNTS = (
    AccountOption(value="1", label="Gmail privat"),
    AccountOption(value="2", label="Arbeit"),
)

VOCABULARY = Vocabulary(
    senders=DIRECTORY, recipients=DIRECTORY, families=FAMILIES, accounts=ACCOUNTS
)


class TestResolvingAName:
    """A name in a question, against the addresses the archive really holds."""

    def test_a_display_name_finds_the_address_behind_it(self) -> None:
        """The question this feature exists for. "Santak" appears nowhere in
        ``info@santak-immobilien.de``'s *local part*; it is how the sender
        signs itself, which is why the directory carries display names at all.
        """
        assert resolve_addresses("meiner Hausverwaltung Santak", DIRECTORY) == (
            "info@santak-immobilien.de",
        )

    def test_a_company_matches_every_address_it_writes_from(self) -> None:
        """Several addresses are the useful reading, not an ambiguity: the
        sender filter means *any of these*, and AWS bills from more than one."""
        assert resolve_addresses("AWS", DIRECTORY) == (
            "no-reply@amazonaws.com",
            "billing@aws.amazon.com",
        )

    def test_a_whole_word_beats_letters_that_merely_appear(self) -> None:
        """``lawsociety.org`` contains *aws*. Without the two tiers it would
        arrive beside the real ones and the picker would show a stranger."""
        assert "anna@lawsociety.org" not in resolve_addresses("AWS", DIRECTORY)

    @pytest.mark.parametrize("typed", ["Müller", "muller", "MÜLLER"])
    def test_an_umlaut_is_found_however_it_was_typed(self, typed: str) -> None:
        """German mail is the target, and a name is the thing most likely to
        carry a diacritic the person searching cannot be bothered to type."""
        assert resolve_addresses(typed, DIRECTORY) == ("anna@lawsociety.org",)

    def test_a_name_the_archive_has_never_seen_matches_nothing(self) -> None:
        """Not a near miss and not a guess. An invented address would filter to
        zero and read as an empty archive."""
        assert resolve_addresses("Frau Schmidt", DIRECTORY) == ()

    def test_words_that_identify_nobody_are_ignored(self) -> None:
        """ "meine" and "GmbH" would each match half a directory by substring."""
        assert resolve_addresses("meine GmbH", DIRECTORY) == ()

    def test_a_mention_matching_more_than_the_picker_shows_is_no_match(self) -> None:
        """A **From** field that silently filled itself with thirty addresses
        would be a filter nobody could read, so the caller is told it did not
        resolve and the words go into the text instead."""
        assert resolve_addresses("com", DIRECTORY, limit=1) == ()
        assert len(resolve_addresses("com", DIRECTORY, limit=2)) == 2


class TestResolvingAFileKind:
    """A person's word for a file, against the families this archive offers."""

    def test_the_german_word_for_an_invoice_finds_the_families_it_arrives_as(
        self,
    ) -> None:
        """An invoice is a PDF or a document, and the filter reads several
        families as *any of these*. ``document`` is dropped here because this
        archive holds no files of that family — the picker never offered it."""
        assert resolve_families(["Rechnungen"], FAMILIES) == (("pdf",), ())

    def test_a_family_is_matched_by_the_label_the_picker_shows(self) -> None:
        """The options handed in are the search form's own, so whatever the
        picker calls a family is a word the question may use for it."""
        assert resolve_families(["Spreadsheets"], FAMILIES) == (("spreadsheet",), ())

    def test_a_word_matching_no_family_comes_back_rather_than_vanishing(self) -> None:
        """The caller puts it into the text. "Vertrag" in an archive whose
        picker offers no document family is still a word worth searching for."""
        assert resolve_families(["Hologramm"], FAMILIES) == ((), ("Hologramm",))


class TestResolvingAMailbox:
    """The one filter that cannot take several values, and what that costs."""

    def test_a_mailbox_is_found_by_the_name_it_was_given(self) -> None:
        assert resolve_account("Gmail", ACCOUNTS) == "1"

    def test_a_mention_matching_two_mailboxes_resolves_to_neither(self) -> None:
        """``SearchFilters.account_id`` is one value, so "my work or my private
        mailbox" has no expressible answer and none is the honest one."""
        two = (
            AccountOption(value="1", label="Mail A"),
            AccountOption(value="2", label="Mail B"),
        )
        assert resolve_account("Mail", two) == ""


class TestTheCalendar:
    """Arithmetic the model is deliberately not asked to do."""

    @pytest.mark.parametrize(
        ("period", "expected"),
        [
            (Period.NONE, ("", "")),
            (Period.LAST_7_DAYS, ("2026-08-31", "2026-09-06")),
            (Period.LAST_30_DAYS, ("2026-08-08", "2026-09-06")),
            (Period.THIS_MONTH, ("2026-09-01", "2026-09-06")),
            (Period.LAST_MONTH, ("2026-08-01", "2026-08-31")),
            (Period.THIS_QUARTER, ("2026-07-01", "2026-09-06")),
            (Period.LAST_QUARTER, ("2026-04-01", "2026-06-30")),
            (Period.THIS_YEAR, ("2026-01-01", "2026-09-06")),
            (Period.LAST_YEAR, ("2025-01-01", "2025-12-31")),
            (Period.LAST_12_MONTHS, ("2025-09-06", "2026-09-06")),
        ],
    )
    def test_every_period_has_one_answer(
        self, period: Period, expected: tuple[str, str]
    ) -> None:
        """Quarter boundaries and month lengths are exactly what a model gets
        quietly wrong, and a wrong window looks like a right one that found
        nothing."""
        assert window_of(period, today=TODAY) == expected

    def test_a_month_end_survives_a_february(self) -> None:
        """31 March back one month is the end of February, not an error."""
        assert window_of(Period.LAST_MONTH, today=date(2024, 3, 31)) == (
            "2024-02-01",
            "2024-02-29",
        )

    def test_twelve_months_back_from_a_leap_day_lands_on_a_real_day(self) -> None:
        assert (
            window_of(Period.LAST_12_MONTHS, today=date(2024, 2, 29))[0] == "2023-02-28"
        )

    def test_an_explicit_date_beats_a_named_period(self) -> None:
        """A question naming both means the more specific of the two."""
        found = resolve(
            QueryIntent(period=Period.LAST_YEAR, after="2026-03-01"),
            VOCABULARY,
            today=TODAY,
        )
        assert (found.after, found.before) == ("2026-03-01", "")

    def test_a_date_centuries_adrift_is_refused_out_loud(self) -> None:
        """``0202-03-01`` for "im März" narrows to nothing, which reads as an
        empty archive rather than as a bad date."""
        found = resolve(QueryIntent(after="0202-03-01"), VOCABULARY, today=TODAY)
        assert found.after == ""
        assert any("outside the archive's range" in one for one in found.notes)

    def test_a_date_that_is_not_a_date_is_refused_out_loud(self) -> None:
        found = resolve(QueryIntent(after="letzten Monat"), VOCABULARY, today=TODAY)
        assert found.after == ""
        assert any("not a date" in one for one in found.notes)

    def test_the_earliest_day_is_a_guard_and_not_a_claim(self) -> None:
        """It says nothing about what the archive holds — only that a filter
        naming a day before it is a model's arithmetic having gone wrong."""
        found = resolve(
            QueryIntent(after=EARLIEST.isoformat()), VOCABULARY, today=TODAY
        )
        assert found.after == EARLIEST.isoformat()


class TestTheThreeQuestions:
    """The samples the feature was specified against, end to end but modelless."""

    def test_alle_aws_nachrichten_mit_rechnungen(self) -> None:
        found = resolve(
            QueryIntent(
                from_mentions=["AWS"],
                has_attachments=True,
                attachment_words=["Rechnungen"],
            ),
            VOCABULARY,
            today=TODAY,
        )
        assert found.senders == ("no-reply@amazonaws.com", "billing@aws.amazon.com")
        assert found.has_attachments is True
        assert found.families == ("pdf",)
        assert found.unresolved == ()

    def test_alle_nachrichten_von_meiner_hausverwaltung_santak(self) -> None:
        found = resolve(
            QueryIntent(from_mentions=["meiner Hausverwaltung Santak"]),
            VOCABULARY,
            today=TODAY,
        )
        assert found.senders == ("info@santak-immobilien.de",)
        assert found.text == ""

    def test_wann_die_letzte_aufladung_fuer_das_telekom_handy(self) -> None:
        """The ordering case. The words are dropped so the structured branch of
        ``search_messages`` answers — which is the only way to get newest-first
        without a sort field — and the note says so, because a word silently
        ignored is a search lying about what it searched.
        """
        found = resolve(
            QueryIntent(
                text="Aufladung", from_mentions=["Telekom"], order=Order.NEWEST
            ),
            VOCABULARY,
            today=TODAY,
        )
        assert found.senders == ("service@telekom.de",)
        assert found.text == ""
        assert any("were not used" in one for one in found.notes)


class TestNothingDisappears:
    """The rule that keeps a narrow question from becoming the whole archive."""

    def test_an_unresolved_sender_becomes_words_and_is_reported(self) -> None:
        found = resolve(
            QueryIntent(text="Vertrag", from_mentions=["Frau Schmidt"]),
            VOCABULARY,
            today=TODAY,
        )
        assert found.senders == ()
        assert found.text == "Vertrag Frau Schmidt"
        assert found.unresolved == ("Frau Schmidt",)

    def test_an_unresolved_mailbox_is_reported_too(self) -> None:
        found = resolve(
            QueryIntent(account_mention="Fastmail"), VOCABULARY, today=TODAY
        )
        assert found.account_id == ""
        assert found.unresolved == ("Fastmail",)

    def test_newest_first_keeps_the_words_when_nothing_else_would_filter(self) -> None:
        """With no structural filter, an empty ``SearchFilters`` is the
        browse-everything read — so honouring the ordering would answer the
        question with the entire archive. The words stay and the note says the
        ordering was not honoured."""
        found = resolve(
            QueryIntent(text="Aufladung", order=Order.NEWEST), VOCABULARY, today=TODAY
        )
        assert found.text == "Aufladung"
        assert any("not necessarily first" in one for one in found.notes)

    def test_one_correspondent_named_twice_is_one_filter(self) -> None:
        """On the recipients' *all* reading a repeat would be a second pattern
        matching the first one's node."""
        found = resolve(
            QueryIntent(from_mentions=["Telekom", "Telekom Kundenservice"]),
            VOCABULARY,
            today=TODAY,
        )
        assert found.senders == ("service@telekom.de",)

    def test_an_empty_question_resolves_to_an_empty_filter(self) -> None:
        """The browse-the-newest read, reached deliberately rather than by a
        swallowed failure — which is why the interpreter refuses a blank
        question before it ever gets here."""
        found = resolve(QueryIntent(), VOCABULARY, today=TODAY)
        assert found.text == ""
        assert found.senders == ()
        assert found.notes == ()
