"""Tests for :mod:`mailarc_ui.message_detail.invite`.

An ``.ics`` in, a meeting out. No archive and no browser: the reader is pure,
which is the whole reason the interesting cases can be written down at all —
a Windows zone name, an exclusive all-day end, a ``DURATION`` where a ``DTEND``
was expected, a cancellation.

Every test that prints a time pins the zone, because the reader converts to
wherever it is running and a suite that did not would pass in Berlin and fail
in a container.
"""

from collections.abc import Iterator

import pytest

from mailarc_ui.message_detail.contentline import read_lines
from mailarc_ui.message_detail.invite import (
    ATTENDEE_LIMIT,
    EVENT_LIMIT,
    NO_TITLE,
    REPEATS,
    CalendarEvent,
    event_of,
    read_invite,
    read_moment,
    read_person,
    recurrence_label,
    when_label,
)

BERLIN = "Europe/Berlin"


@pytest.fixture(autouse=True)
def _berlin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every printed time is in the reader's zone, so the reader gets one.

    ``time.tzset`` is what makes ``TZ`` take effect inside a running process;
    without it ``datetime.astimezone()`` keeps whatever the interpreter read at
    start-up and the assertions below would be about the machine.

    Torn down the way ``test_ui_insights_state`` tears the same trick down:
    ``monkeypatch`` puts ``TZ`` back in the environment but cannot reach the C
    library that already read it, so without the second ``tzset`` every later
    test in this worker keeps running in Berlin.
    """
    import time

    monkeypatch.setenv("TZ", BERLIN)
    time.tzset()
    yield
    time.tzset()  # monkeypatch has put TZ back; make the C library agree


def ics(*lines: str, method: str = "") -> str:
    """A calendar around some ``VEVENT`` lines, written the way a file is."""
    head = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    if method:
        head.append(f"METHOD:{method}")
    return "\r\n".join([*head, "BEGIN:VEVENT", *lines, "END:VEVENT", "END:VCALENDAR"])


def one(*lines: str, method: str = "") -> CalendarEvent:
    """The single event of a calendar built by :func:`ics`."""
    return read_invite(ics(*lines, method=method)).events[0]


class TestWhenAMeetingIs:
    def test_a_meeting_on_one_day_says_the_date_once(self) -> None:
        found = one(
            "DTSTART;TZID=Europe/Berlin:20260910T140000",
            "DTEND;TZID=Europe/Berlin:20260910T153000",
        )
        assert found.when == "Thu, 10.09.2026 14:00 – 15:30"

    def test_a_meeting_that_runs_past_midnight_says_both_dates(self) -> None:
        found = one(
            "DTSTART;TZID=Europe/Berlin:20260910T230000",
            "DTEND;TZID=Europe/Berlin:20260911T010000",
        )
        assert found.when == "Thu, 10.09.2026 23:00 – Fri, 11.09.2026 01:00"

    def test_a_utc_instant_is_shown_where_the_reader_is(self) -> None:
        """``12:00Z`` in September is 14:00 in Berlin, and a mail client shows
        the reader's clock rather than the sender's."""
        found = one("DTSTART:20260910T120000Z", "DTEND:20260910T130000Z")
        assert found.when == "Thu, 10.09.2026 14:00 – 15:00"

    def test_a_whole_day_has_no_clock_on_it(self) -> None:
        found = one("DTSTART;VALUE=DATE:20260910", "DTEND;VALUE=DATE:20260911")
        assert found.when == "Thu, 10.09.2026 (all day)"

    def test_a_two_day_event_does_not_claim_a_third(self) -> None:
        """The end of an all-day event is the first day *not* in it (§3.6.1),
        so printing it as stored adds a day to every workshop."""
        found = one("DTSTART;VALUE=DATE:20260910", "DTEND;VALUE=DATE:20260912")
        assert found.when == "Thu, 10.09.2026 – Fri, 11.09.2026 (all day)"

    def test_a_whole_day_with_no_end_is_still_that_day(self) -> None:
        assert one("DTSTART;VALUE=DATE:20260910").when == "Thu, 10.09.2026 (all day)"

    def test_a_date_with_no_value_parameter_is_still_a_date(self) -> None:
        """Eight characters cannot be a date-time, so the length decides for
        the producers that omit ``VALUE=DATE``."""
        assert one("DTSTART:20260910").when == "Thu, 10.09.2026 (all day)"

    def test_a_meeting_with_no_end_prints_only_its_start(self) -> None:
        assert one("DTSTART:20260910T120000Z").when == "Thu, 10.09.2026 14:00"

    def test_an_event_with_no_start_says_nothing_rather_than_guessing(self) -> None:
        assert one("SUMMARY:Irgendwann").when == ""

    def test_a_date_that_will_not_parse_is_a_missing_date(self) -> None:
        assert one("DTSTART:not-a-date").when == ""

    def test_a_whole_day_that_will_not_parse_is_one_too(self) -> None:
        """Eight characters that are not a date — the other way in."""
        assert one("DTSTART;VALUE=DATE:notadat").when == ""

    def test_a_date_at_the_end_of_time_takes_nothing_down_with_it(self) -> None:
        """``Date: Fri, 31 Dec 9999`` is a routine way of pinning a mail to the
        top of a sort, and an appointment can carry the same. Adding a positive
        offset to it overflows, and one entry must not be able to break the
        panel — the same guard the message header keeps.
        """
        assert one("DTSTART:99991231T235959Z").when == ""


class TestZones:
    def test_a_named_zone_is_applied(self) -> None:
        found = one("DTSTART;TZID=America/New_York:20260910T090000")
        assert found.when == "Thu, 10.09.2026 15:00"

    def test_a_windows_zone_name_keeps_the_wall_clock_and_says_which_zone(self) -> None:
        """Exchange writes ``W. Europe Standard Time``, which no ``zoneinfo``
        has. Printing 14:00 as if it were local would be a confident lie; this
        prints what the sender wrote and whose clock it was."""
        found = one("DTSTART;TZID=W. Europe Standard Time:20260910T140000")
        assert found.when == "Thu, 10.09.2026 14:00 (W. Europe Standard Time)"

    def test_a_floating_time_is_printed_as_written(self) -> None:
        """A time with no zone is 14:00 wherever you are (§3.3.5)."""
        assert one("DTSTART:20260910T140000").when == "Thu, 10.09.2026 14:00"

    def test_a_tzid_that_is_not_even_a_key_is_survived(self) -> None:
        found = one("DTSTART;TZID=../../etc/passwd:20260910T140000")
        assert found.when.startswith("Thu, 10.09.2026 14:00 (")


class TestDurations:
    def test_a_duration_stands_in_for_a_missing_end(self) -> None:
        """How Google Calendar and every CalDAV client write an hour."""
        found = one("DTSTART:20260910T120000Z", "DURATION:PT1H30M")
        assert found.when == "Thu, 10.09.2026 14:00 – 15:30"

    def test_a_duration_in_days_is_read_too(self) -> None:
        found = one("DTSTART:20260910T120000Z", "DURATION:P1DT2H")
        assert found.when == "Thu, 10.09.2026 14:00 – Fri, 11.09.2026 16:00"

    def test_an_explicit_end_wins_over_a_duration(self) -> None:
        found = one(
            "DTSTART:20260910T120000Z",
            "DTEND:20260910T130000Z",
            "DURATION:PT9H",
        )
        assert found.when == "Thu, 10.09.2026 14:00 – 15:00"

    def test_a_duration_with_no_parts_is_not_a_zero_length_meeting(self) -> None:
        assert one("DTSTART:20260910T120000Z", "DURATION:P").when.endswith("14:00")

    def test_nonsense_in_a_duration_leaves_the_start_alone(self) -> None:
        assert one("DTSTART:20260910T120000Z", "DURATION:soon").when.endswith("14:00")


class TestRecurrence:
    @pytest.mark.parametrize(
        ("rule", "said"),
        [
            ("FREQ=DAILY", "Every day"),
            ("FREQ=WEEKLY", "Every week"),
            ("FREQ=MONTHLY", "Every month"),
            ("FREQ=YEARLY", "Every year"),
            ("FREQ=WEEKLY;INTERVAL=2", "Every 2 weeks"),
            ("FREQ=DAILY;INTERVAL=3;BYDAY=MO,TU", "Every 3 days"),
            ("FREQ=WEEKLY;COUNT=10", "Every week, 10 times"),
            ("FREQ=WEEKLY;INTERVAL=1", "Every week"),
        ],
    )
    def test_a_rule_is_said_in_a_few_words(self, rule: str, said: str) -> None:
        assert recurrence_label(rule) == said

    def test_an_until_is_shown_in_the_reader_s_zone(self) -> None:
        assert recurrence_label("FREQ=WEEKLY;UNTIL=20261230T120000Z") == (
            "Every week, until 30.12.2026"
        )

    def test_an_unreadable_frequency_still_says_that_it_repeats(self) -> None:
        """That there *are* others is the fact a single date on screen is
        missing, and it is true even for a rule this cannot read."""
        assert recurrence_label("FREQ=FORTNIGHTLY") == REPEATS
        assert recurrence_label("BYDAY=MO") == REPEATS

    def test_an_unreadable_interval_falls_back_to_one(self) -> None:
        assert recurrence_label("FREQ=WEEKLY;INTERVAL=x") == "Every week"

    def test_a_zero_interval_is_read_as_one(self) -> None:
        assert recurrence_label("FREQ=WEEKLY;INTERVAL=0") == "Every week"

    def test_no_rule_is_no_line(self) -> None:
        assert recurrence_label("") == ""
        assert one("DTSTART:20260910T120000Z").recurrence == ""


class TestPeople:
    def test_an_organiser_is_shown_by_name_with_the_address_behind_it(self) -> None:
        found = one("ORGANIZER;CN=Anna Bauer:mailto:anna@example.com")
        assert found.organizer == "Anna Bauer <anna@example.com>"

    def test_an_organiser_with_no_name_is_shown_once(self) -> None:
        """Not ``anna@example.com <anna@example.com>``."""
        assert one("ORGANIZER:mailto:anna@example.com").organizer == "anna@example.com"

    def test_an_upper_case_scheme_is_still_a_scheme(self) -> None:
        assert one("ORGANIZER:MAILTO:anna@example.com").organizer == "anna@example.com"

    def test_attendees_keep_their_answers(self) -> None:
        found = one(
            'ATTENDEE;CN="Bauer, Anna";PARTSTAT=ACCEPTED:mailto:a@example.com',
            "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:b@example.com",
            "ATTENDEE;PARTSTAT=DECLINED:mailto:c@example.com",
        )
        assert [(one_.label, one_.status) for one_ in found.attendees] == [
            ("Bauer, Anna", "Accepted"),
            ("b@example.com", "No reply"),
            ("c@example.com", "Declined"),
        ]

    def test_an_unknown_answer_is_prettified_rather_than_dropped(self) -> None:
        found = one("ATTENDEE;PARTSTAT=X-UNRESPONSIVE:mailto:a@example.com")
        assert found.attendees[0].status == "X unresponsive"

    def test_an_attendee_with_neither_name_nor_address_is_not_a_person(self) -> None:
        assert one("ATTENDEE;PARTSTAT=ACCEPTED:").attendees == []

    def test_nobody_at_all_is_an_empty_list_and_not_a_none(self) -> None:
        assert one("DTSTART:20260910T120000Z").attendees == []

    def test_a_distribution_list_is_cut_and_says_by_how_much(self) -> None:
        crowd = [
            f"ATTENDEE:mailto:p{index}@example.com"
            for index in range(ATTENDEE_LIMIT + 3)
        ]
        found = one(*crowd)
        assert len(found.attendees) == ATTENDEE_LIMIT
        assert found.more_attendees == "and 3 more"

    def test_a_list_that_fits_says_nothing_about_more(self) -> None:
        assert one("ATTENDEE:mailto:a@example.com").more_attendees == ""

    def test_read_person_of_nothing_is_none(self) -> None:
        assert read_person(None) is None


class TestWhatKindOfNews:
    def test_an_invitation_says_so(self) -> None:
        found = one("SUMMARY:Standup", method="REQUEST")
        assert (found.label, found.label_color) == ("Invitation", "gray")

    def test_a_cancellation_is_the_one_thing_in_red(self) -> None:
        found = one("SUMMARY:Standup", method="CANCEL")
        assert (found.label, found.label_color) == ("Cancelled", "red")

    def test_a_cancelled_occurrence_of_a_live_series_still_reads_cancelled(
        self,
    ) -> None:
        """``METHOD:REQUEST`` with ``STATUS:CANCELLED`` is how one date of a
        recurring meeting is called off, and the cancellation is the news."""
        found = one("STATUS:CANCELLED", method="REQUEST")
        assert found.label == "Cancelled"

    def test_a_tentative_meeting_is_amber(self) -> None:
        found = one("STATUS:TENTATIVE")
        assert (found.label, found.label_color) == ("Tentative", "yellow")

    def test_a_confirmed_meeting_says_nothing_because_they_all_are(self) -> None:
        assert one("STATUS:CONFIRMED", method="PUBLISH").label == ""

    def test_a_reply_is_worth_a_word(self) -> None:
        assert one("SUMMARY:Standup", method="REPLY").label == "Reply"

    def test_an_ordinary_event_wears_no_badge(self) -> None:
        assert one("SUMMARY:Standup").label == ""


class TestWhatTheFileHolds:
    def test_the_ordinary_case_is_one_meeting(self) -> None:
        found = read_invite(ics("SUMMARY:Standup"))
        assert (len(found.events), found.total) == (1, 1)

    def test_the_fields_a_reader_came_for(self) -> None:
        found = one(
            r"SUMMARY:Sprint Review \(Q3\)",
            r"LOCATION:Raum 3.14\, Hauptbau",
            r"DESCRIPTION:Agenda:\n - Demo\n - Retro",
        )
        assert found.summary == "Sprint Review (Q3)"
        assert found.location == "Raum 3.14, Hauptbau"
        assert found.description == "Agenda:\n - Demo\n - Retro"

    def test_an_event_with_no_summary_is_still_called_something(self) -> None:
        assert one("DTSTART:20260910T120000Z").summary == NO_TITLE

    def test_a_reminder_s_own_text_does_not_become_the_meeting_s(self) -> None:
        """A ``VALARM`` carries a ``SUMMARY`` and a ``DESCRIPTION`` of its own."""
        found = one(
            "SUMMARY:Standup",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Erinnerung",
            "END:VALARM",
        )
        assert (found.summary, found.description) == ("Standup", "")

    def test_a_folded_description_is_put_back_together(self) -> None:
        text = ics("DESCRIPTION:Ein sehr langer Text der", " weitergeht")
        assert read_invite(text).events[0].description == (
            "Ein sehr langer Text derweitergeht"
        )

    def test_several_events_all_come_back(self) -> None:
        text = "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                *[
                    line
                    for index in range(3)
                    for line in ("BEGIN:VEVENT", f"SUMMARY:{index}", "END:VEVENT")
                ],
                "END:VCALENDAR",
            ]
        )
        assert [one_.summary for one_ in read_invite(text).events] == ["0", "1", "2"]

    def test_an_exported_calendar_is_cut_and_the_total_still_says_how_many(
        self,
    ) -> None:
        """The panel has to be able to say it is showing the first of many, and
        a list that has already been sliced cannot."""
        text = "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                *[
                    line
                    for index in range(EVENT_LIMIT + 7)
                    for line in ("BEGIN:VEVENT", f"SUMMARY:{index}", "END:VEVENT")
                ],
                "END:VCALENDAR",
            ]
        )
        found = read_invite(text)
        assert (len(found.events), found.total) == (EVENT_LIMIT, EVENT_LIMIT + 7)

    def test_a_calendar_with_no_event_in_it_holds_nothing(self) -> None:
        """A ``VTODO``, a free/busy publication — the caller then prints the
        file as text, which is the whole of this reader's error handling."""
        text = "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nSUMMARY:Milch\r\nEND:VTODO"
        assert read_invite(text).events == []

    def test_nonsense_is_not_an_exception(self) -> None:
        assert read_invite("this is not a calendar").events == []
        assert read_invite("").events == []

    def test_an_event_with_no_calendar_around_it_is_still_an_event(self) -> None:
        """Which is what a truncated download and a hand-written file look like."""
        text = "BEGIN:VEVENT\r\nSUMMARY:Standup\r\nEND:VEVENT"
        assert read_invite(text).events[0].summary == "Standup"


class TestThePiecesOnTheirOwn:
    def test_when_label_of_nothing_is_nothing(self) -> None:
        assert when_label(None, None) == ""

    def test_read_moment_of_nothing_is_none(self) -> None:
        assert read_moment(None) is None

    def test_an_event_built_without_a_method_carries_no_badge(self) -> None:
        """``event_of`` is the seam the panel does not use and a test might."""
        found = event_of(read_lines("SUMMARY:Standup"))
        assert (found.summary, found.label) == ("Standup", "")
