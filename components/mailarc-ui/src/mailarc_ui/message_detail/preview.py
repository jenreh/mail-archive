"""One attachment, projected onto what a panel can show a human.

The header lists the files on a message; pressing one has to answer *what is
in it*, and for the four kinds that carry almost all of a mail archive — a
picture, a PDF, a page of HTML, a page of text — the honest answer is the
thing itself rather than its name and byte count.

Pure functions and one frozen value object, like the rest of this package: the
state reads the original off disk and hands the parsed attachment here, and
everything below is checkable without an archive.

Three decisions are worth stating, because each of them is a road not taken.

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

**Text is decoded as UTF-8 with replacement.** A part's charset is not one of
the things :class:`~mailarc_core.mail.model.ParsedAttachment` keeps, so a
latin-1 CSV shows a replacement character where an umlaut was. That is a
visible flaw in the right direction: the reader sees the file with one
character wrong rather than an error where the file should be.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from mailarc_core.mail.model import ParsedAttachment
from mailarc_ui.message_detail.filetype import FILE_ICONS, FileKind, file_kind
from mailarc_ui.message_detail.model import (
    NO_FILENAME,
    decode_raw,
    frame_document,
    size_label,
)

logger = logging.getLogger(__name__)

PREVIEW_LIMIT = 8 * 1024 * 1024
"""The largest attachment shown at all, in bytes.

Every previewable kind reaches the browser inline — base64 through the state
socket — so this is the point past which showing a file costs more than not
showing it. Eight megabytes is roughly eleven of base64, which a local socket
delivers in well under a second and a scanned twenty-page invoice fits inside.
"""

TEXT_LIMIT = 128 * 1024
"""How much of a text attachment is put on screen, in characters.

A log or an export can be tens of megabytes of one line. What a human reads to
find out what a file is lives at the top of it; the rest is on disk either way.
"""


class PreviewKind(StrEnum):
    """How an attachment is put on screen — one per way of showing it.

    ``NONE`` is not a failure. Most of what arrives on mail is a spreadsheet,
    an archive or a Word document, and a browser cannot render any of them; the
    panel then says what the file is and how big it is, which is the whole of
    what is honestly known.
    """

    IMAGE = "image"
    PDF = "pdf"
    HTML = "html"
    TEXT = "text"
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
        FileKind.CALENDAR: PreviewKind.TEXT,
        FileKind.SPREADSHEET: PreviewKind.NONE,
        FileKind.DOCUMENT: PreviewKind.NONE,
        FileKind.SLIDES: PreviewKind.NONE,
        FileKind.ARCHIVE: PreviewKind.NONE,
        FileKind.AUDIO: PreviewKind.NONE,
        FileKind.VIDEO: PreviewKind.NONE,
        FileKind.OTHER: PreviewKind.NONE,
    }
)
"""What can be drawn for each family — the whole of this module's classifying.

A total map rather than a second set of rules, so the card and the panel can
never disagree about one file. Note the two places where families that share a
glyph part company: a ``.csv`` is a table this can print and an ``.xlsx`` is a
spreadsheet it cannot, and a ``.ics`` invite is text while a ``.pptx`` is not.
That split is exactly why :class:`~mailarc_ui.message_detail.filetype.FileKind`
has more members than :class:`PreviewKind`.

``AUDIO`` and ``VIDEO`` are ``NONE`` on purpose rather than for want of a
player. Every previewable kind here travels to the browser inline, and a media
file that fits in :data:`PREVIEW_LIMIT` is a media file barely worth playing;
a player would also be the first thing on this page that fetches while you
watch it.
"""

TOO_LARGE = "This file is too large to show here; the original is in the archive."
NOT_PREVIEWABLE = "There is no way to show this kind of file in a browser."
EMPTY_PART = "The stored message carries no content for this attachment."
TEXT_CUT = "Only the beginning is shown."
PREVIEW_FAILED = "The original could not be read again to show this file."


class AttachmentPreview(BaseModel):
    """One attachment as the slide-in panel shows it — all strings.

    Four ways of holding content and exactly one of them is filled, chosen by
    :attr:`kind`. Separate fields rather than one ``content``, because what the
    panel does with them differs in kind: :attr:`source` is an ``src``,
    :attr:`document` is an ``srcdoc``, :attr:`text` is a text node. A single
    field would only push the distinction into an ``rx.cond`` anyway, and
    typing it away is what keeps the panel from putting a data URI in a
    paragraph.
    """

    model_config = ConfigDict(frozen=True)

    filename: str = ""
    content_type: str = ""
    size_label: str = ""
    icon: str = "file"
    kind: str = PreviewKind.NONE.value
    """A :class:`PreviewKind` value — plain text, so an ``rx.cond`` can read
    it in the browser."""

    source: str = ""
    """The ``data:`` URI an image or a PDF frame loads."""

    document: str = ""
    """The whole framed document an HTML attachment is shown in."""

    text: str = ""
    note: str = ""
    """One quiet sentence about the file — why there is no preview, or that
    what is shown was cut. Never an error the page has to handle."""


def preview_kind(content_type: str, filename: str | None) -> PreviewKind:
    """How this file should be shown — classified once, mapped once.

    A thin call, and deliberately still a function: the panel asks *what can I
    draw*, and letting it walk the family table itself would be the second
    classifier this module exists to not have.
    """
    return PREVIEW_OF_KIND[file_kind(content_type, filename)]


def data_uri(content_type: str, payload: bytes) -> str:
    """The bytes as something an ``src`` can point at.

    The type is passed through as the sender wrote it rather than re-derived:
    the browser is the one deciding what to do with it, and second-guessing a
    sender who said ``image/jpg`` would only make a picture stop rendering.
    """
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{content_type or 'application/octet-stream'};base64,{encoded}"


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


def preview_of(attachment: ParsedAttachment) -> AttachmentPreview:
    """One parsed attachment, ready for the panel.

    Never raises and never returns ``None``: every file gets a panel, and one
    that cannot be shown gets the sentence saying so. That is what lets the
    component be a plain ``rx.cond`` over :attr:`AttachmentPreview.kind`
    instead of a chain guarding against absence.
    """
    family = file_kind(attachment.content_type, attachment.filename)
    kind = PREVIEW_OF_KIND[family]
    header = AttachmentPreview(
        filename=attachment.filename or NO_FILENAME,
        content_type=attachment.content_type,
        size_label=size_label(attachment.size),
        icon=FILE_ICONS[family],
    )
    if not attachment.payload:
        return header.model_copy(update={"note": EMPTY_PART})
    if len(attachment.payload) > PREVIEW_LIMIT:
        return header.model_copy(update={"note": TOO_LARGE})
    return header.model_copy(update=_content(kind, attachment))


def _content(kind: PreviewKind, attachment: ParsedAttachment) -> dict[str, str]:
    """The one field this kind fills, and the note that goes with it."""
    if kind is PreviewKind.NONE:
        return {"note": NOT_PREVIEWABLE}
    if kind in (PreviewKind.IMAGE, PreviewKind.PDF):
        return {
            "kind": kind.value,
            "source": data_uri(attachment.content_type, attachment.payload),
        }
    text, cut = decode_raw(attachment.payload, TEXT_LIMIT)
    if kind is PreviewKind.HTML:
        return {
            "kind": kind.value,
            "document": frame_document(text),
            "note": TEXT_CUT if cut else "",
        }
    return {
        "kind": kind.value,
        "text": pretty_json(text) if not cut else text,
        "note": TEXT_CUT if cut else "",
    }
