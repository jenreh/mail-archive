"""A vCard attachment read into the contact card it describes.

A ``.vcf`` is somebody's details, and it arrives written for an address book:
``N:Bauer;Anna;;Dr.;`` is a name in five fields, ``ADR`` is an address in
seven, and a photograph is four thousand characters of base64 folded across
fifty lines. Printed verbatim — which is what this panel did before — the one
thing a reader cannot easily find in it is the person.

RFC 6350 (vCard 4.0) and RFC 2426 (3.0) differ in the details this module
cares least about. They share the property names it reads, they share the
structured-value layouts, and they share their whole line grammar with
iCalendar, which is why that part lives in
:mod:`mailarc_ui.message_detail.contentline` and is not written twice. What is
here is only what is about people.

**A photograph is shown only when the file carries it.** ``PHOTO`` may also be
a URL, and following one would make opening an archived contact card fetch
from a stranger's server — the same tracking pixel the mail body's frame exists
to block, wearing a face. So an embedded picture is decoded and drawn, a linked
one is dropped without a word, and the panel falls back to the initials the
rest of this application already draws people with.

Pure, tolerant, no I/O, nothing raises — see
:mod:`mailarc_ui.message_detail.invite`, which is this module's sibling in
every respect including that one.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from mailarc_ui.message_detail.contentline import (
    ContentLine,
    blocks,
    components,
    every,
    read_lines,
    text_of,
    unescape,
)

logger = logging.getLogger(__name__)

NO_NAME = "(unnamed contact)"
"""What a card with neither an ``FN`` nor an ``N`` is called.

Rare and not impossible: a card that carries only an address exists, and a
heading has to say something.
"""

CARD_LIMIT = 25
"""How many contacts one file shows before it stops listing them.

One is the normal case — a signature block, a colleague forwarded. A whole
address book exported from a phone is also a ``.vcf``, is also an attachment,
and is the reason for the number.
"""

DETAIL_LIMIT = 12
"""How many of one kind of detail — addresses, numbers — a card lists.

Nobody has thirteen telephone numbers worth reading; a merged contact record
has forty, all of them the same number written four ways.
"""

PHOTO_LIMIT = 4 * 1024 * 1024
"""The largest embedded picture drawn, in bytes of base64.

Four megabytes of base64 is a three-megabyte photograph, which is far more than
a contact card has ever needed and still small enough to sit inside a panel
that is already carrying the file it came from.
"""

TYPE_WORDS: Mapping[str, str] = MappingProxyType(
    {
        "WORK": "Work",
        "HOME": "Home",
        "CELL": "Mobile",
        "MOBILE": "Mobile",
        "FAX": "Fax",
        "PAGER": "Pager",
        "MAIN": "Main",
        "OTHER": "Other",
        "VIDEO": "Video",
        "TEXTPHONE": "Textphone",
    }
)
"""What a ``TYPE`` is worth printing beside a number or an address.

The list is short because most of what goes in that parameter is not about the
reader. ``VOICE`` is the default for a telephone, ``INTERNET`` the default for
an email address, and ``PREF`` says which one an address book should dial
first; all three are true of nearly every line and so tell a person nothing.
What is left — home, work, mobile, fax — is the distinction somebody actually
scans a contact card for.
"""

PHOTO_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
        "PNG": "image/png",
        "GIF": "image/gif",
        "WEBP": "image/webp",
        "TIFF": "image/tiff",
        "BMP": "image/bmp",
    }
)
"""The bare format names vCard 3.0 puts in ``TYPE`` next to a picture.

vCard 4.0 dropped this in favour of a whole ``data:`` URI, so a modern card
never reaches the table. A 3.0 card says ``TYPE=JPEG`` and means the media type
this maps it to.
"""

DEFAULT_PHOTO_TYPE = "image/jpeg"
"""What an embedded picture with no stated format is assumed to be.

Not a guess so much as a count: a vCard photograph that does not say is a
photograph, and a browser sniffs the bytes for itself anyway — the media type
in a ``data:`` URI mostly has to be *an image* for the element to try.
"""

IMAGE_PREFIX = "data:image/"
"""What a ``PHOTO`` that already carries its own URI has to start with.

A vCard 4.0 photo is a ``data:`` URI written out in full, and this is the one
check made on it: anything else — ``https:``, ``cid:``, ``data:text/html`` —
is not a picture this panel will point an ``<img>`` at.
"""

MAIL_SCHEME = "mailto:"
"""The one scheme an email row is ever given."""

LINK_SCHEMES = ("http://", "https://")
"""The only two schemes a ``URL`` may keep — see :func:`link_href`."""

ADDRESS_FIELDS = 7
"""How many fields an ``ADR`` has: post office box, extended address, street,
locality, region, postal code, country (§6.3.1)."""

NAME_FIELDS = 5
"""How many fields an ``N`` has: family, given, additional, prefixes,
suffixes (§6.2.2)."""


class ContactDetail(BaseModel):
    """One line of a contact card — a number, an address, a link.

    :attr:`label` is the ``TYPE`` in a word and is very often empty, which is
    the whole reason it is a separate field: a card with one email address
    says nothing about it, and a row that printed ``· anna@example.com`` for
    that would be showing punctuation where a fact was missing.
    """

    model_config = ConfigDict(frozen=True)

    label: str = ""
    value: str = ""
    href: str = ""
    """Where pressing this row goes, or ``""`` for a row that is only text.

    Decided here rather than in the component, and that is a safety property
    rather than a style one: the value came out of a file somebody else wrote,
    and whether it may become an ``href`` at all is a question with a wrong
    answer — see :func:`link_href`. A component that built the URL itself would
    be making that decision in a place nothing can test.
    """

    external: bool = False
    """Whether following it leaves the application.

    ``mailto:`` hands off to a mail program and should not also open an empty
    browser tab; a web address should open in one rather than navigating the
    archive away from what the reader was doing.
    """


class ContactCard(BaseModel):
    """One ``VCARD`` as the panel draws it.

    Strings and lists of strings, already printable — the same rule the rest
    of this package follows, for the same reason: §9.1 keeps anything richer
    out of a Reflex state.
    """

    model_config = ConfigDict(frozen=True)

    name: str = NO_NAME
    photo: str = ""
    """An embedded picture as a ``data:`` URI, or ``""``. Never a URL — see the
    module docstring.

    Empty is the ordinary case and not a gap: the avatar the panel draws falls
    back to the initials of :attr:`name`, which is what the mail list and the
    reading pane already draw a person with.
    """

    title: str = ""
    organisation: str = ""
    emails: list[ContactDetail] = []
    phones: list[ContactDetail] = []
    addresses: list[ContactDetail] = []
    links: list[ContactDetail] = []
    birthday: str = ""
    note: str = ""


class ContactFile(BaseModel):
    """What one ``.vcf`` turned out to hold.

    :attr:`total` is the count before :data:`CARD_LIMIT` cut it, for the reason
    :class:`~mailarc_ui.message_detail.invite.CalendarInvite` keeps the same
    field: a list that has already been sliced cannot say that it was.
    """

    model_config = ConfigDict(frozen=True)

    cards: list[ContactCard] = []
    total: int = 0


def read_contacts(text: str) -> ContactFile:
    """One ``.vcf`` file, read into the cards it holds.

    Never raises and never returns ``None``. A file with no ``VCARD`` in it
    comes back with no cards and the caller prints it as text instead, which is
    the same contract :func:`~mailarc_ui.message_detail.invite.read_invite`
    keeps and the reason neither of them needs error handling.
    """
    found = blocks(read_lines(text), "VCARD")
    return ContactFile(
        cards=[card_of(one) for one in found[:CARD_LIMIT]],
        total=len(found),
    )


def card_of(properties: Sequence[ContentLine]) -> ContactCard:
    """One ``VCARD``'s properties as the person a reader sees."""
    return ContactCard(
        name=display_name(properties),
        photo=read_photo(properties),
        title=text_of(properties, "TITLE") or text_of(properties, "ROLE"),
        organisation=organisation_of(properties),
        emails=_details(properties, "EMAIL"),
        phones=_details(properties, "TEL"),
        addresses=_addresses(properties),
        links=_details(properties, "URL"),
        birthday=birthday_label(text_of(properties, "BDAY")),
        note=text_of(properties, "NOTE"),
    )


def display_name(properties: Sequence[ContentLine]) -> str:
    """What the card is headed with.

    ``FN`` first, because it is the name the person wrote out themselves and
    both standards require it. ``N`` is the fallback and has to be assembled —
    ``Bauer;Anna;;Dr.;`` is a family name, a given name, no middle name, a
    prefix and no suffix, and a reader wants *Dr. Anna Bauer* out of it rather
    than the file's own order.
    """
    written = text_of(properties, "FN")
    if written:
        return written
    parts = components(_raw(properties, "N"))
    parts += [""] * (NAME_FIELDS - len(parts))
    family, given, additional, prefixes, suffixes = parts[:NAME_FIELDS]
    ordered = [prefixes, given, additional, family, suffixes]
    return " ".join(one for one in ordered if one) or NO_NAME


def organisation_of(properties: Sequence[ContentLine]) -> str:
    """``ORG`` as one line — a company and the unit inside it.

    The property is structured: ``ORG:Beispiel GmbH;Vertrieb;Nord`` is a
    company and two levels of department, and they read as one line separated
    the way a signature block separates them.
    """
    parts = [one for one in components(_raw(properties, "ORG")) if one]
    return " · ".join(parts)


def read_photo(properties: Sequence[ContentLine]) -> str:
    """The card's own picture as a ``data:`` URI, or ``""``.

    Three shapes and only two of them are pictures this panel will draw. A
    vCard 4.0 photo is already a ``data:`` URI and is passed through once it
    has been checked to be an image; a 3.0 photo is bare base64 with its format
    in ``TYPE``, and is wrapped. A photo that is an ``https:`` URL is neither,
    and is dropped — see the module docstring on why that is a decision rather
    than a gap.
    """
    prop = _first(properties, "PHOTO") or _first(properties, "LOGO")
    if prop is None:
        return ""
    value = "".join(prop.value.split())
    if value.lower().startswith(IMAGE_PREFIX):
        return value if len(value) <= PHOTO_LIMIT else ""
    if not _is_embedded(prop) or len(value) > PHOTO_LIMIT:
        return ""
    if not _is_base64(value):
        return ""
    return f"data:{_photo_type(prop)};base64,{value}"


def birthday_label(value: str) -> str:
    """A ``BDAY`` as a date a person reads, or as it was written.

    ``19850317`` and ``1985-03-17`` are both common and both mean the same day.
    ``--0317`` — a birthday with the year withheld, which vCard 4.0 allows —
    is neither, and comes back untouched rather than as a wrong date or a
    blank: the reader can see what the file says even where this cannot format
    it.
    """
    digits = value.replace("-", "").strip()
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[6:8]}.{digits[4:6]}.{digits[0:4]}"
    return value.strip()


def type_label(prop: ContentLine) -> str:
    """The kind of a number or an address in a word or two — ``Work Fax``.

    Two spellings, because vCard has had two. 3.0 and 4.0 name the parameter
    and give it a comma-separated list, which the line reader has already
    unquoted; 2.1 wrote the words bare — ``TEL;WORK;VOICE:+49…`` — and they
    arrive in :attr:`~mailarc_ui.message_detail.contentline.ContentLine.flags`.
    Both are read, because 2.1 is not a historical curiosity: it is what
    Outlook still exports and what the cards in this archive are written in.

    What survives either way is the entries in :data:`TYPE_WORDS`, each said
    once and in *that table's* order rather than the card's. The order matters
    for exactly one common case and it is worth the line: ``TEL;FAX;WORK``
    would otherwise read *Fax Work*, and the table is written place-first — a
    kind of telephone is a work one before it is a fax.
    """
    stated = [one.strip() for one in prop.param("TYPE").upper().split(",")]
    said = {one for one in (*stated, *prop.flags) if one in TYPE_WORDS}
    words = [TYPE_WORDS[one] for one in TYPE_WORDS if one in said]
    return " ".join(dict.fromkeys(words))


def address_label(prop: ContentLine) -> str:
    """An ``ADR``'s seven fields as one line somebody could post a letter to.

    ``;;Musterstraße 1;Berlin;;10115;Deutschland`` becomes
    ``Musterstraße 1, 10115 Berlin, Deutschland``: the postal code joins the
    town it belongs to rather than becoming a comma-separated field of its own,
    and every empty field simply is not there.
    """
    parts = components(prop.value)
    parts += [""] * (ADDRESS_FIELDS - len(parts))
    box, extended, street, locality, region, code, country = parts[:ADDRESS_FIELDS]
    town = " ".join(one for one in (code, locality) if one)
    lines = [street or box, extended, town, region, country]
    return ", ".join(one for one in lines if one)


def _details(properties: Sequence[ContentLine], name: str) -> list[ContactDetail]:
    """Every line of one kind, each with whatever its ``TYPE`` said."""
    found = [
        _detail(type_label(one), unescape(one.value).strip(), name)
        for one in every(properties, name)
    ]
    return [one for one in found if one.value][:DETAIL_LIMIT]


def _detail(label: str, value: str, name: str) -> ContactDetail:
    """One row, and whether it is somewhere a reader can go.

    Only the two properties that name a destination get one. A telephone
    number could be a ``tel:`` link and is deliberately not: on a desktop it
    hands the number to whichever application once claimed the scheme, which is
    rarely the one the reader expected and never what they meant by clicking.
    """
    if name == "EMAIL":
        return ContactDetail(label=label, value=value, href=mail_href(value))
    if name == "URL":
        return ContactDetail(
            label=label, value=value, href=link_href(value), external=True
        )
    return ContactDetail(label=label, value=value)


def mail_href(address: str) -> str:
    """``mailto:`` for something that is an address, ``""`` for anything else.

    The check is deliberately crude — one ``@``, no spaces — because it is not
    validating an address. It is refusing to put a value into an ``href`` when
    there is no evidence it is one, which is a different and much easier
    question.
    """
    stripped = address.strip()
    if stripped.lower().startswith(MAIL_SCHEME):
        stripped = stripped[len(MAIL_SCHEME) :]
    if not stripped or any(one.isspace() for one in stripped):
        return ""
    return f"{MAIL_SCHEME}{stripped}" if "@" in stripped else ""


def link_href(url: str) -> str:
    """A web address to follow, or ``""`` — and the ``""`` is the point.

    A ``.vcf`` is a file somebody else wrote, and its ``URL`` is whatever they
    put there. Two of the things they could put there are ``javascript:`` and
    ``data:text/html``, and an anchor is one of the few elements that will
    honour either. So the rule is an allow-list rather than a deny-list: a
    value is a link when it is ``http``, when it is ``https``, or when it names
    no scheme at all and is then read as a bare domain — which is how a
    signature block writes ``www.example.com``. Everything else is shown as
    text, which is still the whole truth about what the file said.

    A colon before the first ``/`` is what gives a scheme away, but a port
    wears one too: rejecting every colon dropped ``example.com:8080/page``
    along with ``javascript:``. Only a colon followed by something other than
    digits names a scheme, and the survivors are prefixed with ``https://``
    regardless, so nothing that reaches the anchor can carry one.
    """
    stripped = url.strip()
    if not stripped or any(one.isspace() for one in stripped):
        return ""
    if stripped.lower().startswith(LINK_SCHEMES):
        return stripped
    _, colon, port = stripped.partition("/")[0].partition(":")
    if colon and not port.isdigit():
        return ""
    return f"https://{stripped}" if "." in stripped else ""


def _addresses(properties: Sequence[ContentLine]) -> list[ContactDetail]:
    """The same, for the one property whose value has to be assembled first."""
    found = [
        ContactDetail(label=type_label(one), value=address_label(one))
        for one in every(properties, "ADR")
    ]
    return [one for one in found if one.value][:DETAIL_LIMIT]


def _first(properties: Sequence[ContentLine], name: str) -> ContentLine | None:
    return next((one for one in properties if one.name == name), None)


def _raw(properties: Sequence[ContentLine], name: str) -> str:
    """A structured property's value with its escapes still in place.

    Structured values are split before they are unescaped — see
    :func:`~mailarc_ui.message_detail.contentline.components` — so this is the
    one place that must *not* go through
    :func:`~mailarc_ui.message_detail.contentline.text_of`.
    """
    found = _first(properties, name)
    return "" if found is None else found.value


def _is_embedded(prop: ContentLine) -> bool:
    """Whether the value is the picture itself rather than a link to one.

    ``ENCODING=B`` is what vCard 3.0 says and ``ENCODING=BASE64`` is what
    several address books wrote instead; a ``VALUE=URI`` says outright that it
    is a link, and anything that states no encoding at all is one too.
    """
    return prop.param("ENCODING").upper() in ("B", "BASE64")


def _is_base64(value: str) -> bool:
    """Whether this really decodes, so a broken ``<img>`` is never emitted.

    Worth the decode: a photograph is the one property large enough to be cut
    in half by :data:`~mailarc_ui.message_detail.preview.TEXT_LIMIT`, and half
    a picture is not one.
    """
    try:
        base64.b64decode(value, validate=True)
    except binascii.Error, ValueError:
        logger.debug("Contact photo is not base64")
        return False
    return True


def _photo_type(prop: ContentLine) -> str:
    """The media type of an embedded picture, from what the card called it.

    Both spellings again, for the reason :func:`type_label` reads both: vCard
    2.1 writes ``PHOTO;JPEG;ENCODING=BASE64:``, with the format as a bare word
    rather than as a ``TYPE``.
    """
    stated = prop.param("TYPE").strip()
    if "/" in stated:
        return stated.lower()
    if stated.upper() in PHOTO_TYPES:
        return PHOTO_TYPES[stated.upper()]
    named = next((one for one in prop.flags if one in PHOTO_TYPES), "")
    return PHOTO_TYPES.get(named, DEFAULT_PHOTO_TYPE)
