"""What a reading pane shows, projected onto strings, and the reader it reads.

A :class:`~mailarc_core.mail.model.RenderedMessage` carries datetimes, address
objects and byte counts; a header wants one line of text per field, and §9.1
keeps anything richer out of a Reflex state. So every value object here is
frozen and already printable, and every function is the small piece of
formatting that gets it there — no I/O, so all of it is checkable without a
graph.

The frame document is the exception worth naming. A mail's own HTML goes into a
sandboxed iframe wrapped in a document whose Content-Security-Policy allows
nothing remote: a tracking pixel is a request to a stranger's server the moment
a mail is opened, and a mail client that blocks remote content by default is the
behaviour to copy. The wrapping is a pure function of the markup and one
decision, which is why it lives beside the value objects rather than in a state.
"""

import logging
from datetime import datetime

from appkit_commons.registry import service_registry
from pydantic import BaseModel, ConfigDict

from mailarc_core import ArchiveReader
from mailarc_core.archive.model import MessageLabel
from mailarc_core.mail.model import (
    EmailAddress,
    LabelKind,
    ParsedAttachment,
    RenderedMessage,
)

logger = logging.getLogger(__name__)

RAW_LIMIT = 256 * 1024
"""How much of an original the viewer shows, in characters.

A reader wants the headers and the first part of the body; the megabytes of
base64 behind them are on disk either way and nothing a human reads.
"""

NO_SUBJECT = "(no subject)"

TAB_MESSAGE = "message"
TAB_SOURCE = "source"
"""The two ways of looking at one mail; the tab bar's values."""

FRAME_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"
)
"""What a rendered mail may load: its own inline pictures and styles, nothing
remote. Scripts, frames, forms and fetches of any kind are out."""

FRAME_CSP_REMOTE = (
    "default-src 'none'; img-src data: https: http:; "
    "style-src 'unsafe-inline' https: http:; font-src data: https:; "
    "media-src https:"
)
"""The same policy after a human allowed remote content: pictures, styles and
fonts may now come from elsewhere. Scripts, frames, forms and connections
stay out — trust extends to being seen, never to being run."""

FRAME_INK = "#1a1a1a"
FRAME_PAPER = "#ffffff"
"""``--ma-text`` and ``--ma-surface``, written out — the only two literals in
this package, and the reason is the document boundary.

A custom property defined on the parent page's ``:root`` does not cross into an
iframe: the frame is its own document with its own cascade, so a ``var(--ma-…)``
in :data:`FRAME_STYLE` would resolve to nothing and the declaration would be
dropped. The values are therefore restated here rather than referenced, which
also means they are **fixed**: a mail renders on white in dark mode too. That is
arguably right — a mail was written to be read on paper-white and re-tinting
somebody's HTML is a change to their document — but it is a decision, not an
oversight, and it is written down here rather than left to be discovered.
"""

FRAME_STYLE = (
    ":root{color-scheme:light}"
    "body{margin:0;padding:16px;font:14px/1.5 -apple-system,BlinkMacSystemFont,"
    f"'Segoe UI',Helvetica,Arial,sans-serif;color:{FRAME_INK};"
    f"background:{FRAME_PAPER};"
    "word-wrap:break-word}img{max-width:100%}"
)
"""A quiet default look for mails that bring no styling of their own.

``color-scheme: light`` is what stops the browser from darkening form controls
and scrollbars inside a frame it has been told is on white."""


LABEL_COLORS = {
    LabelKind.USER: "blue",
    LabelKind.FOLDER: "teal",
    LabelKind.SYSTEM: "gray",
}
"""A chip's colour says where the label came from: a human's stand out,
the provider's own housekeeping stays quiet."""

_SYSTEM_PREFIX = "CATEGORY_"


class LabelChip(BaseModel):
    """One label on a list row — the text to print and the colour to print it in."""

    model_config = ConfigDict(frozen=True)

    text: str
    color: str

    @classmethod
    def from_label(cls, label: MessageLabel) -> LabelChip:
        return cls(
            text=label_text(label.name, label.kind),
            color=LABEL_COLORS[label.kind],
        )


class AttachmentRow(BaseModel):
    """One file on the message, as the header strip lists it."""

    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str
    size_label: str

    @classmethod
    def from_attachment(cls, attachment: ParsedAttachment) -> AttachmentRow:
        return cls(
            filename=attachment.filename or "(unnamed)",
            content_type=attachment.content_type,
            size_label=size_label(attachment.size),
        )


class MessageView(BaseModel):
    """One message the way the readable tab shows it — all strings.

    ``body_html`` is the whole frame document, CSP and base style included,
    ready for an ``srcdoc``; empty when the mail has no HTML and the text
    body is what shows.
    """

    model_config = ConfigDict(frozen=True)

    subject: str = ""
    sender: str = ""
    sender_address: str = ""
    """The bare address — the key a trust decision is recorded under."""
    recipients: str = ""
    """The ``To`` line. Not named ``to`` — that is a method on every reflex Var."""
    cc: str = ""
    date: str = ""
    body_html: str = ""
    """The mail's own markup, unframed; the state wraps it per policy."""
    body_text: str = ""
    remote_references: int = 0
    attachments: list[AttachmentRow] = []

    @classmethod
    def from_rendered(cls, rendered: RenderedMessage) -> MessageView:
        return cls(
            subject=rendered.subject or NO_SUBJECT,
            sender=address_label(rendered.sender),
            sender_address=rendered.sender.address if rendered.sender else "",
            recipients=", ".join(address_label(one) for one in rendered.to),
            cc=", ".join(address_label(one) for one in rendered.cc),
            date=long_date_label(rendered.sent_at),
            body_html=rendered.body_html or "",
            body_text=rendered.body_text,
            remote_references=rendered.remote_references,
            attachments=[
                AttachmentRow.from_attachment(one) for one in rendered.attachments
            ],
        )


def archive_reader() -> ArchiveReader:
    """The reader the composition root published. Call inside a method only."""
    try:
        return service_registry().get(ArchiveReader)
    except KeyError as error:
        raise RuntimeError(
            "No archive reader is registered — did app.composition run?"
        ) from error


def label_text(name: str, kind: LabelKind) -> str:
    """What a chip prints for a label.

    A human's label — or a folder — is shown exactly as they named it. The
    provider's own come as constants, ``CATEGORY_PROMOTIONS`` and ``INBOX``,
    and a mail client prints those as "Promotions" and "Inbox".
    """
    if kind is not LabelKind.SYSTEM:
        return name
    return name.removeprefix(_SYSTEM_PREFIX).replace("_", " ").title()


def long_date_label(sent_at: datetime | None) -> str:
    """The full date a header shows, in the local zone."""
    local = _local(sent_at)
    return "" if local is None else local.strftime("%a, %d.%m.%Y %H:%M")


def _local(sent_at: datetime | None) -> datetime | None:
    """One stored instant in the reader's own zone, or ``None``.

    ``None`` covers two cases a header renders the same way: no date at all,
    and a date the local zone cannot express. The second is not hypothetical — a
    ``Date:`` header is whatever a sender wrote and nothing range-checks it, so
    ``Date: Fri, 31 Dec 9999 23:59:59 +0000`` (a routine way of pinning a mail
    to the top of a date sort) reaches the archive intact and then overflows
    ``datetime.max`` the moment a positive UTC offset is added. Year 0001 does
    the same against ``datetime.min`` west of UTC. One archived message must
    not be able to take the whole list down with it.
    """
    if sent_at is None:
        return None
    try:
        return sent_at.astimezone()
    except OverflowError, ValueError, OSError:
        logger.warning("Date outside the range this zone can print: %r", sent_at)
        return None


def address_label(address: EmailAddress | None) -> str:
    """``Name <address>`` when there is a name, the address alone otherwise."""
    if address is None:
        return ""
    if address.display_name:
        return f"{address.display_name} <{address.address}>"
    return address.address


_SIZE_UNITS = ("B", "KB", "MB", "GB")


def size_label(size: int) -> str:
    """A byte count the way a file list prints it: ``12 KB``, ``1.3 MB``."""
    value = float(max(size, 0))
    unit = _SIZE_UNITS[0]
    for unit in _SIZE_UNITS:
        if value < 1024 or unit == _SIZE_UNITS[-1]:
            break
        value /= 1024
    return (
        f"{value:.0f} {unit}" if unit == "B" or value >= 10 else f"{value:.1f} {unit}"
    )


def frame_document(body_html: str, *, allow_remote: bool = False) -> str:
    """Wrap a mail's HTML in the document the sandboxed frame loads.

    The policy comes first so it governs everything after it, the base style
    second so the mail's own styles win over it. The mail may well bring its
    own ``<html>`` and ``<head>``; browsers fold a second pair into the first,
    which is what every webmail relies on too. ``allow_remote`` swaps in the
    policy a human agreed to; scripts stay out under either.
    """
    policy = FRAME_CSP_REMOTE if allow_remote else FRAME_CSP
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
        f"<style>{FRAME_STYLE}</style></head><body>{body_html}</body></html>"
    )


def decode_raw(data: bytes, limit: int = RAW_LIMIT) -> tuple[str, bool]:
    """Bytes to text for the viewer, and whether the cut was applied.

    Lenient on purpose — a replaced character is a fact about the message, a
    decode error would hide the whole of it. The cut lands after decoding so
    a multi-byte character is never split.
    """
    text = data.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text, False
    return text[:limit], True
