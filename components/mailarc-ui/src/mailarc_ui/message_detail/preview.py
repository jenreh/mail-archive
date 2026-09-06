"""One attachment, projected onto what a panel can show a human.

The header lists the files on a message; pressing one has to answer *what is
in it*, and for the four kinds that carry almost all of a mail archive — a
picture, a PDF, a page of HTML, a page of text — the honest answer is the
thing itself rather than its name and byte count.

Pure functions and one frozen value object, like the rest of this package: the
state reads the original off disk and hands the parsed attachment here, and
everything below is checkable without an archive.

Four decisions are worth stating, because each of them is a road not taken.

**Two kinds of file are read rather than rendered.** A ``.ics`` and a ``.vcf``
are the only attachments a mail carries that are *already* structured — a
meeting and a person, written down in a grammar — and showing them the way
everything else here is shown means printing that grammar at a reader. So they
go through :mod:`mailarc_ui.message_detail.invite` and
:mod:`mailarc_ui.message_detail.vcard` and arrive as records the panel lays
out. The cost is a parser per format and the honesty that goes with one: both
fall back to printing the file when they cannot read it.

**The bytes travel as a ``data:`` URI, not from an endpoint.** The obvious
alternative is an HTTP route that streams one attachment, and it would be
cheaper for a large file. It would also be a URL that serves archived mail,
reachable by anything that can reach the app — a new surface on an application
whose whole security posture so far is *the browser never fetches anything*.
A ``data:`` URI is carried by the socket that is already open, works unchanged
in the desktop shell where there is no second origin to talk to, and costs
nothing to reason about. :data:`PREVIEW_LIMIT` is what keeps that affordable.

**A PDF gets a frame of its own and not the sandbox the mail body uses.**
Measured, not assumed: a browser renders a PDF with an internal viewer that
counts as a plugin, and ``<iframe sandbox="">`` blocks plugins — a PDF inside
the sandboxed ``srcdoc`` frame this module builds for HTML renders as a blank
rectangle in Chromium. It therefore goes into a frame pointed straight at the
``data:`` URI, which is an **opaque origin**: the document inside cannot reach
the page around it, which is the property that mattered about the sandbox.

**What a file *is* is somebody else's question.**
:mod:`mailarc_ui.message_detail.filetype` classifies it, and
:data:`PREVIEW_OF_KIND` says what can be drawn for each family. This module
decides only how to draw it.

**Text is decoded by trying, not by believing.** UTF-8 first, then whatever
the part said its charset was, then Windows-1252 with replacement so that there
is always a string — see
:func:`~mailarc_ui.message_detail.model.decode_raw`, which explains why the
sender's own statement is asked second rather than first. This used to be a
plain UTF-8 decode with replacement, and a contact card written by a German
mail program read ``Gesch�ftsleitung``.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from mailarc_core.mail.model import ParsedAttachment
from mailarc_ui.message_detail.filetype import (
    FileFacts,
    FileKind,
    file_facts,
)
from mailarc_ui.message_detail.invite import CalendarEvent, read_invite
from mailarc_ui.message_detail.model import (
    NO_FILENAME,
    decode_raw,
    frame_document,
    size_label,
)
from mailarc_ui.message_detail.vcard import ContactCard, read_contacts

logger = logging.getLogger(__name__)

PREVIEW_LIMIT = 30 * 1024 * 1024
"""The largest attachment shown at all, in bytes.

Every previewable kind reaches the browser inline — base64 through the state
socket — so this is the point past which showing a file costs more than not
showing it.

Thirty megabytes is roughly forty of base64. That is a lot to put on a socket,
and it is the right number anyway: the figure this started at, eight, was set
when the only things drawn here were a picture and a PDF frame, and a real
deck with photographs on its slides is routinely three or four times that. A
limit that refuses the documents people actually send is not protecting
anything — it is just the feature not working. The socket is local in the
desktop shell and on the same host in the web one, and the decode happens once
per opened file.
"""

TEXT_LIMIT = 128 * 1024
"""How much of a text attachment is put on screen, in characters.

A log or an export can be tens of megabytes of one line. What a human reads to
find out what a file is lives at the top of it; the rest is on disk either way.
"""


class PreviewKind(StrEnum):
    """How an attachment is put on screen — one per way of showing it.

    ``NONE`` is not a failure. An archive, a video, a Word 97 document — the
    panel then says what the file is and how big it is, which is the whole of
    what is honestly known.

    The three Office members are named after the families they serve rather
    than after a file extension. They are not interchangeable: each is a
    different renderer with a different prop surface, which is why they are
    three members and not one ``OFFICE`` with a format beside it.

    ``CALENDAR`` and ``CONTACT`` are the two shapes that are not the file
    itself. Everything else here puts the bytes on screen and lets the reader
    read them, because the bytes are already something a person reads. A
    ``.ics`` and a ``.vcf`` are forms filled in for a machine, and printing one
    verbatim — which is what this did until
    :mod:`mailarc_ui.message_detail.invite` and
    :mod:`mailarc_ui.message_detail.vcard` were written — asks a person to find
    ``DTSTART`` in eighty lines to learn when the meeting is.
    """

    IMAGE = "image"
    PDF = "pdf"
    HTML = "html"
    TEXT = "text"
    CALENDAR = "calendar"
    CONTACT = "contact"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    SLIDES = "slides"
    NONE = "none"


PREVIEW_OF_KIND: Mapping[FileKind, PreviewKind] = MappingProxyType(
    {
        FileKind.IMAGE: PreviewKind.IMAGE,
        FileKind.PDF: PreviewKind.PDF,
        FileKind.WEB: PreviewKind.HTML,
        FileKind.TEXT: PreviewKind.TEXT,
        FileKind.DATA: PreviewKind.TEXT,
        FileKind.TABLE: PreviewKind.TEXT,
        FileKind.MAIL: PreviewKind.TEXT,
        FileKind.CALENDAR: PreviewKind.CALENDAR,
        FileKind.CONTACT: PreviewKind.CONTACT,
        FileKind.SPREADSHEET: PreviewKind.SPREADSHEET,
        FileKind.DOCUMENT: PreviewKind.DOCUMENT,
        FileKind.SLIDES: PreviewKind.SLIDES,
        FileKind.ARCHIVE: PreviewKind.NONE,
        FileKind.AUDIO: PreviewKind.NONE,
        FileKind.VIDEO: PreviewKind.NONE,
        FileKind.OTHER: PreviewKind.NONE,
    }
)
"""What can be drawn for each family — the whole of this module's classifying.

A total map rather than a second set of rules, so the card and the panel can
never disagree about one file. Note where families that share a glyph part
company: a ``.csv`` is a table this prints as text and an ``.xlsx`` goes to a
spreadsheet renderer, and a ``.ics`` invite is read into an event while a
``.pptx`` is a deck. That split is why
:class:`~mailarc_ui.message_detail.filetype.FileKind` still has more members
than :class:`PreviewKind`.

**Nor is it the last word on a calendar or a contact.** ``CALENDAR`` and
``CONTACT`` say that the file *claims* to be one; :func:`_content` then reads
it, and a file with nothing in it — a ``VTODO``, a free/busy publication, a
download that stopped halfway, a ``.vcf`` that is really a note — falls back to
``TEXT`` and is printed. A reader is better served by the lines of a file
nothing could parse than by an empty card saying a meeting has no title, no
time and nobody on it.

**This map is not the last word on the three Office families.** It says which
renderer a ``DOCUMENT`` would go to; :func:`preview_kind_of` then reads
:attr:`~mailarc_ui.message_detail.filetype.FileFacts.ooxml` to find out whether
this particular file is one that renderer can parse, and sends a ``.doc`` or a
``.pages`` back to ``NONE``. Keeping the map total and the narrowing separate
is what lets the map stay a table anyone can read.

``AUDIO`` and ``VIDEO`` are ``NONE`` on purpose rather than for want of a
player. Every previewable kind here travels to the browser inline, and a media
file that fits in :data:`PREVIEW_LIMIT` is a media file barely worth playing;
a player would also be the first thing on this page that fetches while you
watch it.
"""

TOO_LARGE = "This file is too large to show here; the original is in the archive."
NOT_PREVIEWABLE = "There is no way to show this kind of file in a browser."
OLD_OFFICE = "Only the modern .docx, .xlsx and .pptx formats can be shown here."
EMPTY_PART = "The stored message carries no content for this attachment."
TEXT_CUT = "Only the beginning is shown."
PREVIEW_FAILED = "The original could not be read again to show this file."
EVENTS_CUT = "This calendar holds more events; only the first are shown."
CARDS_CUT = "This file holds more contacts; only the first are shown."


class AttachmentPreview(BaseModel):
    """One attachment as the slide-in panel shows it.

    Several ways of holding content and exactly one of them is filled, chosen
    by :attr:`kind`. Separate fields rather than one ``content``, because what
    the panel does with them differs in kind: :attr:`source` is an ``src``,
    :attr:`document` is an ``srcdoc``, :attr:`text` is a text node,
    :attr:`events` is a list something iterates. A single field would only push
    the distinction into an ``rx.cond`` anyway, and typing it away is what
    keeps the panel from putting a data URI in a paragraph.

    ``extra="forbid"`` is the guard that makes that typing mean something.
    This model used to be assembled with ``model_copy(update=…)``, which
    validates *nothing*: a wrong type went through, and a key with a typo in it
    — ``contact`` for ``contacts`` — became a phantom attribute and drew an
    empty panel with no error raised anywhere. Every field is optional and the
    six content fields are only ever set from one place, so a rejected key here
    is always a mistake and never a caller being clever.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str = ""
    content_type: str = ""
    size_label: str = ""
    icon: str = "file"
    kind: str = PreviewKind.NONE.value
    """A :class:`PreviewKind` value — plain text, so an ``rx.cond`` can read
    it in the browser."""

    source: str = ""
    """The ``data:`` URI an image loads."""

    encoded: str = ""
    """The bytes as base64 with no ``data:`` envelope — what a viewer decodes
    into an ``ArrayBuffer``.

    A fifth way of holding content rather than a reuse of :attr:`source`, for
    the reason this class's docstring gives about the other four: what the
    panel does with it differs in kind. ``source`` is an ``src``; this is a
    React prop, and handing a viewer a ``data:`` URI to slice a prefix off
    would put the one rule about how bytes are framed inside a JSX file, where
    nothing in Python can check it.
    """

    document: str = ""
    """The whole framed document an HTML attachment is shown in."""

    text: str = ""

    events: list[CalendarEvent] = []
    contacts: list[ContactCard] = []
    """What a ``.ics`` or a ``.vcf`` turned out to be about.

    The two fields here that are not strings, and they earn it: an event has
    seven parts a panel draws in seven places and a contact card has nine, and
    flattening either into one block of text would be the raw file again with
    better spacing. Reflex serialises a ``BaseModel``, so ``rx.foreach``
    resolves ``event.summary`` in the browser the way the header's attachment
    strip already resolves a row.
    """

    note: str = ""
    """One quiet sentence about the file — why there is no preview, or that
    what is shown was cut. Never an error the page has to handle."""

    def with_note(self, note: str) -> AttachmentPreview:
        """The same file, with one sentence put under its heading.

        A method rather than ``model_copy(update={"note": …})`` at four call
        sites: ``model_copy`` skips validation entirely, so every one of those
        was a place a typo could not be caught. This one spells the field name
        once and goes through a validator.

        ``dict(self)`` and not ``model_dump()``: iterating a model yields its
        fields with the values still *as models*, where dumping would turn an
        event into a dictionary and hand the panel something ``rx.foreach``
        resolves nothing on. Validation puts them back either way; not taking
        them apart is cheaper and says what is meant.
        """
        return AttachmentPreview.model_validate({**dict(self), "note": note})

    def released(self) -> AttachmentPreview:
        """The same file with its bytes dropped — see
        :meth:`~mailarc_ui.message_detail.state.MessageDetailState.close_attachment`.

        What is kept is what the panel still draws on its way out: the name,
        the type, the size, the glyph. What goes is every field that can hold
        megabytes.
        """
        return AttachmentPreview(
            filename=self.filename,
            content_type=self.content_type,
            size_label=self.size_label,
            icon=self.icon,
            note=self.note,
        )


OFFICE_KINDS = frozenset(
    {PreviewKind.DOCUMENT, PreviewKind.SPREADSHEET, PreviewKind.SLIDES}
)
"""The three shapes whose renderers read OOXML and nothing else."""

VIEWER_KINDS = OFFICE_KINDS | {PreviewKind.PDF}
"""The shapes drawn by a renderer that takes bytes rather than a URL.

Derived rather than listed again, which it was until the two sets drifted
apart by one edit's worth of forgetting. Every Office shape goes to a viewer
that wants an ``ArrayBuffer``; the PDF is the one member that is here without
being an OOXML question, and writing it as *the Office three plus the PDF* is
the whole of the relationship.

The PDF did not used to be on this list: it was an ``<iframe src>`` at a
``data:`` URI, pointed at the browser's own plugin. The viewer that replaced
that takes an ``ArrayBuffer``, so carrying the URI as well would put eight
megabytes on the socket twice.
"""


def preview_kind_of(facts: FileFacts) -> PreviewKind:
    """How this file should be shown, given what it already turned out to be.

    The narrowing is the one step :data:`PREVIEW_OF_KIND` cannot express. It is
    keyed by family, and a family is a coarser thing than a renderer can be:
    ``SLIDES`` holds a ``.pptx`` and a ``.key``, and only the first of them is
    a zip full of OOXML. So an Office shape is asked one more question —
    :attr:`~mailarc_ui.message_detail.filetype.FileFacts.ooxml`, which the same
    pass that settled the family already answered.
    """
    kind = PREVIEW_OF_KIND[facts.family]
    if kind in OFFICE_KINDS and not facts.ooxml:
        return PreviewKind.NONE
    return kind


def preview_kind(content_type: str, filename: str | None) -> PreviewKind:
    """How this file should be shown — classified once, mapped once, narrowed
    once.

    A thin call, and deliberately still a function: the panel asks *what can I
    draw*, and letting it walk the family table itself would be the second
    classifier this module exists to not have. A caller that also wants the
    glyph or the word should ask
    :func:`~mailarc_ui.message_detail.filetype.file_facts` once and go through
    :func:`preview_kind_of` instead of classifying the same file twice.
    """
    return preview_kind_of(file_facts(content_type, filename))


def encoded_bytes(payload: bytes) -> str:
    """The bytes as base64 — the one place this module spells that.

    ASCII rather than UTF-8 on the decode because base64 is ASCII by
    definition, and saying so is what makes a non-ASCII byte here an error
    rather than a silently different string.
    """
    return base64.b64encode(payload).decode("ascii")


def data_uri(content_type: str, payload: bytes) -> str:
    """The bytes as something an ``src`` can point at.

    The type is passed through as the sender wrote it rather than re-derived:
    the browser is the one deciding what to do with it, and second-guessing a
    sender who said ``image/jpg`` would only make a picture stop rendering.
    """
    kind = content_type or "application/octet-stream"
    return f"data:{kind};base64,{encoded_bytes(payload)}"


def pretty_json(text: str) -> str:
    """JSON laid out over lines, or the text untouched when it is not JSON.

    A machine-written export is one line of several thousand characters, which
    is the one text file a reader genuinely cannot read as it stands. Anything
    that does not parse is shown exactly as it arrived — a broken export is
    still worth looking at, and often the reason for looking.
    """
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError, RecursionError:
        return text


class PreviewContent(TypedDict, total=False):
    """The fields one shape fills — the return of :func:`_content`.

    A ``TypedDict`` and not a plain ``dict[str, …]``, because this mapping is
    unpacked straight into :class:`AttachmentPreview` and a plain dict made
    that a place with no checking on either side: the key names went unchecked
    statically and ``model_copy`` went on to skip validation at runtime too.
    Every key here is optional — that is the whole point of the class, one
    shape fills one field — which is what ``total=False`` says.
    """

    kind: str
    source: str
    encoded: str
    document: str
    text: str
    events: list[CalendarEvent]
    contacts: list[ContactCard]
    note: str


def preview_of(attachment: ParsedAttachment) -> AttachmentPreview:
    """One parsed attachment, ready for the panel.

    Never raises and never returns ``None``: every file gets a panel, and one
    that cannot be shown gets the sentence saying so. That is what lets the
    component be a plain ``rx.cond`` over :attr:`AttachmentPreview.kind`
    instead of a chain guarding against absence.

    The file is classified exactly once, here, and the answer is carried down.
    This used to ask :func:`~mailarc_ui.message_detail.filetype.file_kind`
    three times for one attachment — once for the glyph, once inside
    :func:`preview_kind`, once more inside :func:`_no_preview_note` — which is
    two more opinions than a file should have about what it is.
    """
    facts = file_facts(attachment.content_type, attachment.filename)
    header = AttachmentPreview(
        filename=attachment.filename or NO_FILENAME,
        content_type=attachment.content_type,
        size_label=size_label(attachment.size),
        icon=facts.icon,
    )
    if not attachment.payload:
        return header.with_note(EMPTY_PART)
    if len(attachment.payload) > PREVIEW_LIMIT:
        return header.with_note(TOO_LARGE)
    return AttachmentPreview.model_validate(
        {**dict(header), **_content(preview_kind_of(facts), facts, attachment)}
    )


def _no_preview_note(facts: FileFacts) -> str:
    """Why there is no preview, said as precisely as the file allows.

    A reader holding a ``.doc`` is told the format is the problem and which
    ones are not, because that is something they can act on — re-save it, or
    ask the sender. The generic sentence is kept for a zip or a video, where
    the format is not the point and there is nothing to do about it.
    """
    if PREVIEW_OF_KIND[facts.family] in OFFICE_KINDS:
        return OLD_OFFICE
    return NOT_PREVIEWABLE


def _content(
    kind: PreviewKind, facts: FileFacts, attachment: ParsedAttachment
) -> PreviewContent:
    """The one field this kind fills, and the note that goes with it.

    Text is the floor rather than one branch among several: a calendar with no
    event and a contact file with no card both fall through to it, and the raw
    lines of a file this could not read are a better answer than a card with
    nothing on it. Note that the two record branches are ``if`` and not
    ``elif`` on purpose: an unparseable one is *meant* to reach the bottom.
    """
    if kind is PreviewKind.NONE:
        return {"note": _no_preview_note(facts)}
    if kind is PreviewKind.IMAGE:
        return {
            "kind": kind.value,
            "source": data_uri(attachment.content_type, attachment.payload),
        }
    if kind in VIEWER_KINDS:
        return {"kind": kind.value, "encoded": encoded_bytes(attachment.payload)}
    text, cut = decode_raw(attachment.payload, TEXT_LIMIT, attachment.charset)
    if kind is PreviewKind.HTML:
        return {
            "kind": kind.value,
            "document": frame_document(text),
            "note": TEXT_CUT if cut else "",
        }
    if kind is PreviewKind.CALENDAR:
        invite = read_invite(text)
        if invite.events:
            return {
                "kind": kind.value,
                "events": invite.events,
                "note": _record_note(
                    invite.total, len(invite.events), EVENTS_CUT, cut=cut
                ),
            }
    if kind is PreviewKind.CONTACT:
        contacts = read_contacts(text)
        if contacts.cards:
            return {
                "kind": kind.value,
                "contacts": contacts.cards,
                "note": _record_note(
                    contacts.total, len(contacts.cards), CARDS_CUT, cut=cut
                ),
            }
    return {
        "kind": PreviewKind.TEXT.value,
        "text": pretty_json(text) if not cut else text,
        "note": TEXT_CUT if cut else "",
    }


def _record_note(total: int, shown: int, cut_note: str, *, cut: bool) -> str:
    """Which of the two ways a read file can be incomplete happened, if either.

    The record count is asked first because it is the one a reader can act on —
    an exported calendar or address book is a file they can open in a program
    that handles the whole of it. A cut is the rarer case by far:
    :data:`TEXT_LIMIT` is a hundred and twenty-eight kilobytes and a meeting
    invitation is two.
    """
    if total > shown:
        return cut_note
    return TEXT_CUT if cut else ""
