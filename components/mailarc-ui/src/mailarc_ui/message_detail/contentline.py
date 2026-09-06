"""The line grammar iCalendar and vCard share, read once for both.

``BEGIN:VCALENDAR`` and ``BEGIN:VCARD`` are the same sentence in the same
language. RFC 6350 says so outright — vCard borrows its whole surface syntax
from RFC 5545 — and the two formats agree line for line on all of it: the
``NAME;PARAM=value:value`` shape, the folding of a long line onto the next one,
the five backslash escapes inside a text value, the quoting that lets a
parameter contain a colon, and ``BEGIN``/``END`` marking a block.

They part company entirely above that. A ``VEVENT`` has a ``DTSTART`` and a
``VCARD`` has an ``FN``, and neither module has any business knowing about the
other's properties — which is exactly the seam this module is cut along. It
knows what a *line* is; :mod:`~mailarc_ui.message_detail.invite` and
:mod:`~mailarc_ui.message_detail.vcard` know what the lines *mean*.

Written when the second format arrived and not before. The first version of
this lived inside ``invite.py``, where it was right — one caller is not an
abstraction. What moved it here was ``.vcf``, and what would have happened
otherwise is the thing this repository's own architecture notes complain about
elsewhere: two copies of a parser, drifting.

Pure and tolerant, like its two callers: no I/O, nothing raises, and a line
that will not parse is a line neither of them has.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

TEXT_ESCAPES: Mapping[str, str] = MappingProxyType(
    {"n": "\n", "N": "\n", "\\": "\\", ",": ",", ";": ";"}
)
"""The five escapes a text value may carry — RFC 5545 §3.3.11, RFC 6350 §3.4.

Anything else after a backslash is not an escape, and the tolerant reading —
drop the backslash, keep the character — is what a sender who wrote a Windows
path into a note meant either way.
"""


class ContentLine(BaseModel):
    """One unfolded line, split into the three things it is made of.

    ``DTSTART;TZID=Europe/Berlin:20260906T140000`` is the name ``DTSTART``, the
    parameter ``TZID``, and the value; ``TEL;TYPE=CELL:+49…`` is the same
    shape. Names and parameter names are upper-cased on the way in because both
    standards say they are case-insensitive and mail programs take them at
    their word.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: str
    params: Mapping[str, str] = Field(default_factory=dict)
    """A factory and not a shared ``{}`` or a ``MappingProxyType``: pydantic
    deep-copies a field default on every validation, and a mapping proxy cannot
    be copied — which is a ``TypeError`` on the first line that has no
    parameters, meaning almost all of them."""

    flags: tuple[str, ...] = ()
    """Parameters written as a bare word, with no ``=`` and no name.

    vCard 2.1 wrote every type that way — ``TEL;WORK;VOICE:+49…`` — and it is
    still the version Outlook exports and mail carries. Kept separately rather
    than guessed into ``params`` because *this* module has no idea what a bare
    ``WORK`` is a parameter *of*: that is a question about vCard, and
    :func:`~mailarc_ui.message_detail.vcard.type_label` is where it is asked.
    Recording them without interpreting them is the whole seam.
    """

    def param(self, name: str) -> str:
        """One parameter's value, or ``""`` — never a ``KeyError``."""
        return self.params.get(name, "")


def unfold(text: str) -> list[str]:
    """The content lines, with the standard's line folding undone.

    A producer splits a long line at 75 octets and begins the next one with a
    single space or tab; undoing that is the first thing any reader of either
    format has to do, because a folded ``DESCRIPTION`` is most of what a
    meeting invitation consists of and a folded ``PHOTO`` is all of what a
    contact's picture is. ``CRLF`` and bare ``LF`` are both accepted — the
    standards require the first and mail carries plenty of the second.
    """
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def read_line(line: str) -> ContentLine | None:
    """One content line split into name, parameters and value, or ``None``.

    ``None`` for a blank line and for anything with no ``:`` in it, which is
    the shape a truncated file ends in.

    A parameter with no ``=`` in it lands in :attr:`ContentLine.flags` rather
    than in ``params``, unnamed and uninterpreted — see that field for why the
    two are kept apart.
    """
    head, found, value = split_once(line, ":")
    if not found:
        return None
    name, *parameters = split_all(head, ";")
    if not name.strip():
        return None
    return ContentLine(
        name=name.strip().upper(),
        value=value,
        params=dict(parameter(one) for one in parameters if "=" in one),
        flags=tuple(
            one.strip().upper() for one in parameters if "=" not in one and one.strip()
        ),
    )


def read_lines(text: str) -> list[ContentLine]:
    """Every line of a file that parses, in order."""
    return [one for line in unfold(text) if (one := read_line(line)) is not None]


def blocks(lines: Iterable[ContentLine], component: str) -> list[list[ContentLine]]:
    """Each ``BEGIN:<component>`` block's own lines, nesting respected.

    A stack of names and not a flag, because the nesting is the whole point. A
    ``VALARM`` inside a ``VEVENT`` carries its own ``SUMMARY`` and
    ``DESCRIPTION``, and a reader that collected properties by name alone would
    put the reminder's text where the meeting's belongs; a ``VTIMEZONE``'s
    ``STANDARD`` block has a ``DTSTART`` of its own and is skipped by the same
    rule.

    Only lines *directly* inside the named block are kept — a nested block's
    are neither collected nor stitched into the parent — and a block whose
    ``END`` never arrives keeps everything to the end of the file, which is the
    useful reading of a download that stopped early.
    """
    found: list[list[ContentLine]] = []
    stack: list[str] = []
    for one in lines:
        if one.name == "BEGIN":
            stack.append(one.value.strip().upper())
            if stack[-1] == component:
                found.append([])
        elif one.name == "END":
            if stack:
                stack.pop()
        elif stack and stack[-1] == component and found:
            found[-1].append(one)
    return found


def outer(lines: Iterable[ContentLine], name: str, within: str = "") -> str:
    """A property's value from outside every block — a calendar's ``METHOD``.

    Depth-aware for the reason :func:`blocks` is: ``METHOD`` belongs to the
    ``VCALENDAR`` and a ``VEVENT`` inside it may carry properties of its own
    with any name at all.

    ``within`` names the block the property must sit directly inside, for the
    caller that knows. Depth alone cannot tell a calendar-level ``METHOD``
    from an event-level one in a file that never opened a ``VCALENDAR``: a
    fragment beginning ``BEGIN:VEVENT`` puts its own properties at depth one,
    so a ``METHOD`` there was read as the calendar's and cancelled an event
    nothing had cancelled. A property at depth zero belongs to no block at all
    and is always answered.
    """
    stack: list[str] = []
    for one in lines:
        if one.name == "BEGIN":
            stack.append(one.value.strip().upper())
        elif one.name == "END":
            if stack:
                stack.pop()
        elif one.name == name and _is_outer(stack, within):
            return one.value.strip()
    return ""


def _is_outer(stack: Sequence[str], within: str) -> bool:
    """Whether the current nesting counts as the file's outermost level."""
    if not stack:
        return True
    return len(stack) == 1 and (not within or stack[0] == within.upper())


def first(lines: Iterable[ContentLine], name: str) -> ContentLine | None:
    """The first line with this name, or ``None``."""
    return next((one for one in lines if one.name == name), None)


def every(lines: Iterable[ContentLine], name: str) -> list[ContentLine]:
    """Every line with this name, in the order the file wrote them."""
    return [one for one in lines if one.name == name]


def text_of(lines: Iterable[ContentLine], name: str) -> str:
    """A text property's value with its escapes resolved, or ``""``."""
    found = first(lines, name)
    return "" if found is None else unescape(found.value).strip()


def unescape(value: str) -> str:
    """A text value with ``\\n``, ``\\,``, ``\\;`` and ``\\\\`` resolved.

    One pass rather than four replacements, which is not a micro-optimisation
    but the only correct way to do it: ``\\\\n`` is a backslash followed by the
    letter n, and any pair of sequential replacements turns it into a newline.
    """
    out: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            following = value[index + 1]
            out.append(TEXT_ESCAPES.get(following, following))
            index += 2
            continue
        out.append(character)
        index += 1
    return "".join(out)


def components(value: str, separator: str = ";") -> list[str]:
    """A structured value split into its parts, escapes resolved after.

    ``N:Bauer;Anna;;Dr.;`` is five fields and ``ADR`` is seven, and the split
    has to happen before the unescaping rather than after: a semicolon inside
    one of those fields is written ``\\;`` precisely so that it is not a
    separator, and unescaping first would turn it back into one.
    """
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            current.append(value[index : index + 2])
            index += 2
            continue
        if character == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
        index += 1
    parts.append("".join(current))
    return [unescape(one).strip() for one in parts]


def parameter(text: str) -> tuple[str, str]:
    """``TZID=Europe/Berlin`` as a pair, with any quotes taken off the value."""
    name, _, value = text.partition("=")
    return name.strip().upper(), value.strip().strip('"')


def split_once(text: str, separator: str) -> tuple[str, bool, str]:
    """Split once at the first separator that is not inside a quoted value.

    The quoting matters for exactly one thing and it is common: a parameter
    value may be quoted precisely so that it can contain a colon or a
    semicolon — ``ATTENDEE;CN="Bauer, Anna";…`` and Exchange's
    ``ALTREP="cid:…"`` — and a reader that split on the first colon would take
    half a parameter for the value of the property.
    """
    quoted = False
    for index, character in enumerate(text):
        if character == '"':
            quoted = not quoted
        elif character == separator and not quoted:
            return text[:index], True, text[index + 1 :]
    return text, False, ""


def split_all(text: str, separator: str) -> list[str]:
    """The same split, repeated to the end of the string."""
    parts: list[str] = []
    rest = text
    while True:
        head, found, rest = split_once(rest, separator)
        parts.append(head)
        if not found:
            return parts
