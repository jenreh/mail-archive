"""An iCalendar attachment read into the few facts a person wants from one.

A ``.ics`` file is a meeting, and the panel used to print it the way it prints
a log — ``DTSTART;TZID=Europe/Berlin:20260906T140000`` and eighty more lines of
the same. Everything the reader opened the file for is in there and every bit
of it is spelled for a machine: *when*, *where*, *who*, and whether the thing
is still on at all.

So this module reads RFC 5545 far enough to answer those four, and stops.
It does not expand a recurrence into its occurrences, does not resolve an
``RRULE`` against its ``EXDATE`` list, does not keep ``VTODO``, ``VJOURNAL`` or
``VFREEBUSY``, and does not attempt to be a calendar. A preview that showed a
reader the *third* occurrence of a series would be doing arithmetic nobody
asked it to do and would be wrong about half of it; a preview that says
*Every 2 weeks* beside the first date is telling them what the file says.

The line grammar is :mod:`mailarc_ui.message_detail.contentline`'s, shared with
the vCard reader. What is left here is only the part that is about calendars.

Pure and dependency-free, like the modules either side of it: text in, frozen
value objects out, every field already printable — §9.1 keeps anything richer
out of a Reflex state, and it is also what lets the whole of this be checked
without a browser or an archive. The one thing it reaches for outside the
standard library is :mod:`zoneinfo`, which is in it.

**Tolerant on the way in, honest on the way out.** Mail carries the ``.ics``
files that mail programs write, which means folded lines, ``\\n`` escapes,
Windows zone names no ``zoneinfo`` has heard of, a ``DURATION`` where a
``DTEND`` was expected, and the occasional file with no ``VCALENDAR`` around
it. None of that is an error worth showing a reader, so nothing here raises: a
property that will not parse is a property this module does not have, and an
event with nothing in it is an event with an empty line where that fact goes.
The one thing it will not do is invent — an unresolvable ``TZID`` prints the
wall clock the sender wrote *and the zone name they wrote it in*, rather than
quietly claiming that 14:00 in an unknown zone is 14:00 here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from mailarc_ui.message_detail.contentline import (
    ContentLine,
    blocks,
    every,
    first,
    outer,
    parameter,
    read_lines,
    text_of,
)

logger = logging.getLogger(__name__)

NO_TITLE = "(no title)"
"""What an event with no ``SUMMARY`` is called.

Its own constant rather than an empty heading, and shaped like
:data:`~mailarc_ui.message_detail.model.NO_SUBJECT` on purpose: a mail with no
subject and a meeting with no title are the same absence, and a reader who has
seen one should recognise the other.
"""

ALL_DAY = "(all day)"
RANGE_DASH = " – "
"""An en dash with spaces around it, which is what a date range is set in.
A hyphen would read as part of the date it follows."""

DATE_FORMAT = "%a, %d.%m.%Y"
TIME_FORMAT = "%H:%M"
"""The date and the clock, split so a range can print one date and two times.

Deliberately the same shape
:func:`~mailarc_ui.message_detail.model.long_date_label` gives a message,
because the two sit inches apart — the mail is dated in the header and the
meeting inside it is dated in the panel, and two spellings of a Tuesday would
read as two different kinds of fact.
"""

ATTENDEE_LIMIT = 24
"""How many people one event names before the rest become a count.

A meeting is read by looking for *whether I am on this and who else is*, and
both are answered in the first two dozen names. Behind that number is a
distribution list of four hundred, which is a scroll bar rather than an answer
— and four hundred rows through the state socket for a file somebody glanced
at.
"""

EVENT_LIMIT = 25
"""How many events one file contributes before the panel stops listing them.

A meeting invitation holds one. A calendar somebody exported holds every
appointment they have ever had, arrives as a ``.ics`` like any other, and is
the reason this number exists.
"""


class CalendarPerson(BaseModel):
    """One name on an event — the organiser, or somebody invited.

    :attr:`label` is what gets printed and :attr:`address` is what it stands
    for, kept apart for the reason
    :class:`~mailarc_ui.message_detail.model.AddressRow` keeps them apart: a
    person who gave a display name is shown by it, and the address behind it
    is still the thing that identifies them.
    """

    model_config = ConfigDict(frozen=True)

    label: str = ""
    address: str = ""
    status: str = ""
    """Their answer, in a word — ``Accepted``, ``Declined``, ``No reply``.
    Empty when the file does not say, which is normal outside a reply."""


class CalendarEvent(BaseModel):
    """One ``VEVENT`` as the panel draws it — every field already printable.

    Empty strings rather than ``None`` throughout, which is what lets the
    component be a row per field guarded by one ``rx.cond`` on that field
    instead of a chain of checks against absence.
    """

    model_config = ConfigDict(frozen=True)

    summary: str = NO_TITLE
    when: str = ""
    """The whole of the time, as one line — see :func:`when_label`."""

    recurrence: str = ""
    location: str = ""
    description: str = ""
    organizer: str = ""
    """The organiser as one line, ``Anna Bauer <anna@example.com>``.

    A string where :attr:`attendees` holds people, because it is one value on
    one row and a reader never scans it; the list is scanned, and a list of
    strings could not carry each person's answer beside their name.
    """

    attendees: list[CalendarPerson] = []
    more_attendees: str = ""
    """``and 37 more`` when the list was cut at :data:`ATTENDEE_LIMIT`."""

    label: str = ""
    """One word over the title when there is something to say about the whole
    event — ``Cancelled``, ``Invitation``, ``Reply``. Empty for the ordinary
    case, where the event's existence is the whole of the news."""

    label_color: str = "gray"
    """The badge's colour, decided here rather than where it is drawn.

    Matching a status in a component would mean switching on a ``Var`` in the
    browser — the same seam :mod:`mailarc_ui.accounts.state` documents where it
    keeps its own status table.
    """


class CalendarInvite(BaseModel):
    """What one ``.ics`` file turned out to hold.

    :attr:`total` is the count *before* :data:`EVENT_LIMIT` cut it, and it is
    the whole reason this wrapper exists rather than a bare list: the panel has
    to be able to say that it is showing the first twenty-five of three
    hundred, and a list that has already been sliced cannot say so.
    """

    model_config = ConfigDict(frozen=True)

    events: list[CalendarEvent] = []
    total: int = 0


class Moment(BaseModel):
    """One end of an event, and everything printing it needs to know.

    Three fields because a ``DTSTART`` is three different things depending on
    how it was written, and the difference is visible to a reader.
    :attr:`all_day` decides whether a clock is printed at all, and
    :attr:`zone_note` is the zone name that could not be resolved — see the
    module docstring on why that is shown rather than swallowed.
    """

    model_config = ConfigDict(frozen=True)

    at: datetime
    all_day: bool = False
    zone_note: str = ""


STATUS_WORDS: Mapping[str, str] = MappingProxyType(
    {"CANCELLED": "Cancelled", "TENTATIVE": "Tentative"}
)
"""What an event's own ``STATUS`` is worth saying out loud.

``CONFIRMED`` is missing on purpose: it is the default state of every meeting
in a mailbox, and a badge that says so on all of them says nothing on any of
them.
"""

METHOD_WORDS: Mapping[str, str] = MappingProxyType(
    {
        "REQUEST": "Invitation",
        "CANCEL": "Cancelled",
        "REPLY": "Reply",
        "COUNTER": "Counter-proposal",
        "DECLINECOUNTER": "Counter declined",
    }
)
"""What the ``METHOD`` on the calendar means for the reader (§3.7.2).

``PUBLISH`` and ``ADD`` are missing for the reason ``CONFIRMED`` is: an
exported calendar is ``PUBLISH`` and there is nothing to tell anybody about
that. ``CANCEL`` maps to the same word ``STATUS:CANCELLED`` does, because a
cancellation usually carries both and a reader must not be shown two badges
saying one thing.
"""

LABEL_COLORS: Mapping[str, str] = MappingProxyType(
    {"Cancelled": "red", "Tentative": "yellow", "Counter-proposal": "yellow"}
)
"""The badge colours that are not the quiet default.

A cancellation is the one thing in this panel a reader must not scroll past,
so it is the one thing wearing red.
"""

PARTSTAT_WORDS: Mapping[str, str] = MappingProxyType(
    {
        "ACCEPTED": "Accepted",
        "DECLINED": "Declined",
        "TENTATIVE": "Tentative",
        "NEEDS-ACTION": "No reply",
        "DELEGATED": "Delegated",
        "COMPLETED": "Completed",
        "IN-PROCESS": "In process",
    }
)
"""What a ``PARTSTAT`` says about one person (§3.2.12).

``NEEDS-ACTION`` becomes *No reply* rather than being passed through: it is
the state of everyone who has not answered yet, it is what an invitation looks
like the moment it arrives, and *needs action* reads as an instruction to the
person reading the archive.
"""

FREQUENCY_WORDS: Mapping[str, str] = MappingProxyType(
    {
        "SECONDLY": "second",
        "MINUTELY": "minute",
        "HOURLY": "hour",
        "DAILY": "day",
        "WEEKLY": "week",
        "MONTHLY": "month",
        "YEARLY": "year",
    }
)
"""The unit each ``FREQ`` counts in, so an interval reads as English.

Stored as the singular and pluralised with an ``s`` where the interval is
greater than one — every one of the seven takes it, which is why there is no
table of plurals.
"""

REPEATS = "Repeats"
"""The recurrence line for a rule whose ``FREQ`` is missing or unknown.

Still worth a line: *that* the thing repeats is the fact a reader needs from a
single date on screen, and it is true even when the rule is not one this
module can read.
"""

DURATION_PATTERN = re.compile(
    r"^(?P<sign>[+-])?P"
    r"(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
"""``PT1H30M``, ``P2W``, ``-PT15M`` — the duration form of §3.3.6.

Read only where a ``DTEND`` is missing, which is how Google Calendar and every
CalDAV client write a one-hour meeting.
"""


def read_invite(text: str) -> CalendarInvite:
    """One ``.ics`` file, read into what a panel can draw.

    Never raises and never returns ``None``. A file with no ``VEVENT`` in it —
    a bare ``VTODO``, a free/busy publication, a truncated download, something
    that was never a calendar — comes back with no events, and the caller shows
    it as text instead. That is the whole of the error handling, and it is why
    there is none anywhere below.
    """
    lines = read_lines(text)
    found = blocks(lines, "VEVENT")
    method = outer(lines, "METHOD", within="VCALENDAR").upper()
    return CalendarInvite(
        events=[event_of(one, method) for one in found[:EVENT_LIMIT]],
        total=len(found),
    )


def event_of(properties: Sequence[ContentLine], method: str = "") -> CalendarEvent:
    """One ``VEVENT``'s properties as the event a person reads.

    ``method`` belongs to the calendar rather than to the event and is passed
    down because the panel draws events: *this is a cancellation* is the first
    thing a reader needs and there is nowhere else they would look for it.
    """
    start = read_moment(first(properties, "DTSTART"))
    label = _label(method, text_of(properties, "STATUS").upper())
    attendees = [
        person
        for prop in every(properties, "ATTENDEE")
        if (person := read_person(prop)) is not None
    ]
    return CalendarEvent(
        summary=text_of(properties, "SUMMARY") or NO_TITLE,
        when=when_label(start, _end(properties, start)),
        recurrence=recurrence_label(text_of(properties, "RRULE")),
        location=text_of(properties, "LOCATION"),
        description=text_of(properties, "DESCRIPTION"),
        organizer=_person_label(read_person(first(properties, "ORGANIZER"))),
        attendees=attendees[:ATTENDEE_LIMIT],
        more_attendees=_overflow(len(attendees)),
        label=label,
        label_color=LABEL_COLORS.get(label, "gray"),
    )


def when_label(start: Moment | None, end: Moment | None) -> str:
    """The whole of an event's time as the one line a reader scans.

    ``Sun, 06.09.2026 14:00 – 15:30`` when it begins and ends on one day, with
    the date said once because saying it twice is how a form prints a range and
    not how a person does. ``Sun, 06.09.2026 (all day)`` where there is no
    clock to print.

    The exclusive end is the subtlety worth naming. An all-day event that runs
    for one day is written ``DTSTART;VALUE=DATE:20260906`` with
    ``DTEND;VALUE=DATE:20260907`` — the end is the first day *not* in it
    (§3.6.1) — so printing the stored end would tell a reader that a one-day
    workshop takes two.
    """
    if start is None:
        return ""
    if start.all_day:
        return _all_day_label(start, end)
    return _timed_label(start, end)


def read_moment(prop: ContentLine | None) -> Moment | None:
    """A ``DTSTART`` or ``DTEND`` as the instant it names, in the reader's zone.

    Four things one property can be, and the difference between them is the
    difference between a correct panel and a confident wrong one:

    * ``20260906`` — a whole day. No clock, and no zone to convert through.
    * ``20260906T140000Z`` — an instant. Shown where the reader is.
    * ``…;TZID=Europe/Berlin:20260906T140000`` — an instant, once the zone is
      resolved. Also shown where the reader is.
    * ``20260906T140000`` with no zone at all — a *floating* time (§3.3.5),
      which means 14:00 wherever you happen to be. Printed as written.

    The third has a failure mode worth the code it costs: Exchange writes
    ``TZID=W. Europe Standard Time``, which is a Windows zone name and not an
    IANA one, so :class:`~zoneinfo.ZoneInfo` cannot find it. The wall clock is
    then printed as the sender wrote it with their zone name beside it, which
    is exactly as much as is known.
    """
    if prop is None:
        return None
    value = prop.value.strip()
    if _is_date_only(prop, value):
        return _day_moment(value)
    return _instant(value, prop.param("TZID").strip())


def recurrence_label(rule: str) -> str:
    """An ``RRULE`` said in a few words — ``Every 2 weeks, until 31.12.2026``.

    Not an expansion and not a full reading of §3.3.10: a rule can carry
    ``BYDAY``, ``BYSETPOS`` and ``WKST``, and rendering *the last working day of
    every second month* in English is a paragraph of code that would still be
    wrong for the next rule. What a reader needs from a single date on screen
    is that there are others and roughly how often, and that is ``FREQ``,
    ``INTERVAL`` and whichever of ``UNTIL`` or ``COUNT`` says where it stops.
    """
    if not rule:
        return ""
    parts = dict(parameter(one) for one in rule.split(";") if "=" in one)
    return _cadence(parts) + _horizon(parts)


def read_person(prop: ContentLine | None) -> CalendarPerson | None:
    """An ``ORGANIZER`` or one ``ATTENDEE`` as somebody with a name.

    The value is a calendar address — ``mailto:anna@example.com`` in every file
    this archive will ever see — and the display name rides in ``CN``. A person
    with no ``CN`` is shown by their address, which is the same fallback
    :func:`~mailarc_ui.message_detail.model.sender_label` makes for a mail.
    """
    if prop is None:
        return None
    address = _mail_address(prop.value)
    label = prop.param("CN").strip() or address
    if not label:
        return None
    return CalendarPerson(
        label=label,
        address=address,
        status=_partstat(prop.param("PARTSTAT")),
    )


def _all_day_label(start: Moment, end: Moment | None) -> str:
    first_day = start.at.date()
    last = _last_day(end)
    if last is None or last <= first_day:
        return f"{_date_text(start.at)} {ALL_DAY}"
    return f"{_date_text(start.at)}{RANGE_DASH}{last.strftime(DATE_FORMAT)} {ALL_DAY}"


def _last_day(end: Moment | None) -> date | None:
    """The last day actually inside an all-day event — see :func:`when_label`."""
    return None if end is None else end.at.date() - timedelta(days=1)


def _timed_label(start: Moment, end: Moment | None) -> str:
    opening = f"{_date_text(start.at)} {start.at.strftime(TIME_FORMAT)}"
    if end is None:
        return opening + _zone_suffix(start)
    closing = (
        end.at.strftime(TIME_FORMAT)
        if end.at.date() == start.at.date()
        else f"{_date_text(end.at)} {end.at.strftime(TIME_FORMAT)}"
    )
    return opening + RANGE_DASH + closing + _zone_suffix(start)


def _zone_suffix(start: Moment) -> str:
    """The zone the sender named, printed only when it could not be applied."""
    return f" ({start.zone_note})" if start.zone_note else ""


def _date_text(moment: datetime) -> str:
    return moment.strftime(DATE_FORMAT)


def _is_date_only(prop: ContentLine, value: str) -> bool:
    """Whether this is a whole day rather than a time on one.

    The ``VALUE=DATE`` parameter is the standard's answer and most producers
    send it; the length is the answer for the ones that do not, and it cannot
    collide — a date-time is never eight characters long.
    """
    return prop.param("VALUE").upper() == "DATE" or len(value) == 8


def _day_moment(value: str) -> Moment | None:
    """A whole day, which has no zone and must not be given one.

    The linter's objection is right everywhere else in this application and
    wrong here: an all-day event is the 6th of September in whatever zone you
    read it in, and attaching one would move a birthday across a date line.
    """
    try:
        moment = datetime.strptime(value, "%Y%m%d")  # noqa: DTZ007 - a day, not an instant
        return Moment(at=moment, all_day=True)
    except ValueError:
        logger.debug("Not a calendar date: %r", value)
        return None


def _instant(value: str, tzid: str) -> Moment | None:
    naive = _naive(value)
    if naive is None:
        return None
    if value.endswith("Z"):
        return _local(naive.replace(tzinfo=ZoneInfo("UTC")))
    if not tzid:
        return Moment(at=naive)
    zone = _zone(tzid)
    if zone is None:
        return Moment(at=naive, zone_note=tzid)
    return _local(naive.replace(tzinfo=zone))


def _naive(value: str) -> datetime | None:
    """The wall clock the file wrote, before any zone is applied to it.

    Deliberately naive, and the callers are what make it not stay that way: a
    value ending in ``Z`` gets UTC, one with a resolvable ``TZID`` gets that
    zone, and what is left is a *floating* time (§3.3.5) which has no zone by
    definition and is printed as written.
    """
    try:
        return datetime.strptime(  # noqa: DTZ007 - the zone arrives in _instant
            value.removesuffix("Z"), "%Y%m%dT%H%M%S"
        )
    except ValueError:
        logger.debug("Not a calendar date-time: %r", value)
        return None


def _zone(tzid: str) -> ZoneInfo | None:
    """The named zone, or ``None`` for one this machine has never heard of.

    Two unrelated failures mean the same thing here: a lookup that finds
    nothing — which is what a Windows zone name does, and
    :class:`~zoneinfo.ZoneInfoNotFoundError` is a ``KeyError`` — and a ``TZID``
    ``zoneinfo`` refuses to treat as a path at all.
    """
    try:
        return ZoneInfo(tzid)
    except KeyError, ValueError, OSError:
        logger.debug("Unknown calendar zone: %r", tzid)
        return None


def _local(moment: datetime) -> Moment | None:
    """One known instant in the reader's own zone.

    Guarded for the reason :func:`~mailarc_ui.message_detail.model._local`
    guards the same call: a date near either end of the range overflows the
    moment an offset is applied, and one appointment must not be able to take
    the panel down with it.
    """
    try:
        return Moment(at=moment.astimezone())
    except OverflowError, ValueError, OSError:
        logger.warning("Calendar date outside the range this zone can print")
        return None


def _cadence(parts: Mapping[str, str]) -> str:
    unit = FREQUENCY_WORDS.get(parts.get("FREQ", "").upper(), "")
    if not unit:
        return REPEATS
    interval = _interval(parts.get("INTERVAL", ""))
    return f"Every {unit}" if interval == 1 else f"Every {interval} {unit}s"


def _interval(value: str) -> int:
    """The ``INTERVAL``, or 1 — which is what the standard says it defaults to
    and also what an unreadable one is safest read as."""
    try:
        return max(int(value), 1)
    except ValueError:
        return 1


def _horizon(parts: Mapping[str, str]) -> str:
    """Where the series stops, when the rule says.

    ``UNTIL`` is a date-time in its own right and is read through the same
    function every other one goes through, so a rule that ends at a UTC instant
    prints its end in the reader's zone like everything else.
    """
    until = read_moment(ContentLine(name="UNTIL", value=parts.get("UNTIL", "")))
    if until is not None:
        return f", until {until.at.strftime('%d.%m.%Y')}"
    count = parts.get("COUNT", "")
    return f", {count} times" if count.isdigit() else ""


def _label(method: str, status: str) -> str:
    """The one word over the title, when the two sources agree on one.

    The event's own ``STATUS`` is asked first: a series where the third
    occurrence was called off arrives as ``METHOD:REQUEST`` with
    ``STATUS:CANCELLED`` on the exception, and the cancellation is the news.
    """
    return STATUS_WORDS.get(status, "") or METHOD_WORDS.get(method, "")


def _end(properties: Sequence[ContentLine], start: Moment | None) -> Moment | None:
    """Where the event ends, however this file chose to say it.

    A ``DTEND`` if there is one; otherwise ``DTSTART`` plus ``DURATION``, which
    is how a CalDAV client writes an hour. Neither, and the panel prints a
    start and no range — which is right, because a ``VEVENT`` with neither
    lasts a day if it is dated and an instant if it is timed (§3.6.1), and
    printing the second of those as a range would be inventing an end time.
    """
    ending = read_moment(first(properties, "DTEND"))
    if ending is not None:
        return ending
    span = _duration(text_of(properties, "DURATION"))
    if span is None or start is None:
        return None
    return start.model_copy(update={"at": start.at + span})


def _duration(value: str) -> timedelta | None:
    """``PT1H30M`` as a span of time, or ``None`` for anything else.

    A bare ``P`` matches the shape and carries no components, which is not a
    duration; it comes back ``None`` rather than as a zero-length event.
    """
    found = DURATION_PATTERN.match(value.strip().upper())
    if found is None:
        return None
    parts = {
        key: int(one) for key, one in found.groupdict().items() if one and key != "sign"
    }
    if not parts:
        return None
    span = timedelta(**parts)
    return -span if found["sign"] == "-" else span


def _mail_address(value: str) -> str:
    """The address inside a calendar address, without its scheme."""
    stripped = value.strip()
    return stripped[7:] if stripped[:7].lower() == "mailto:" else stripped


def _partstat(value: str) -> str:
    """One person's answer in a word, including for answers not in the table.

    An unknown ``PARTSTAT`` is prettified rather than dropped: it is a value
    somebody's calendar server invented and it still means something to them,
    and ``X-UNRESPONSIVE`` read as *X unresponsive* is closer to the truth than
    a blank.
    """
    upper = value.strip().upper()
    if not upper:
        return ""
    return PARTSTAT_WORDS.get(upper, upper.replace("-", " ").capitalize())


def _person_label(person: CalendarPerson | None) -> str:
    """``Anna Bauer <anna@example.com>``, the address alone, or nothing.

    The same three cases and the same shape
    :func:`~mailarc_ui.message_detail.model.address_label` gives a mail
    header's sender, because the organiser of a meeting is a sender by another
    name and the two lines are read one under the other.
    """
    if person is None:
        return ""
    if person.address and person.label != person.address:
        return f"{person.label} <{person.address}>"
    return person.label


def _overflow(total: int) -> str:
    """``and 37 more`` — or nothing, which is the usual answer."""
    return f"and {total - ATTENDEE_LIMIT} more" if total > ATTENDEE_LIMIT else ""
