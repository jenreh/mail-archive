"""Reflex state for looking at what the import wrote: a list, and one original.

The archive answers through :class:`~mailarc_core.ArchiveReader`, which the
composition root builds and leaves in the service registry — ``mailarc-ui``
may not import ``app`` (§4.1), so it reads the reader out the same way the
accounts page reads its providers, and never at import time.

What this adds is the projection. A
:class:`~mailarc_core.archive.model.MessageSummary` carries a datetime and two
halves of a sender; a list row wants one name and one short date label, and
§9.1 keeps anything richer out of a Reflex state. The raw message is text by
the time it is a state var: decoded leniently, because a mailbox holds every
encoding ever mislabelled, and capped, because a browser does not want a
forty-megabyte attachment rendered as base64.

The readable view is the same bytes through
:func:`~mailarc_core.mail.render_message`, projected once more onto strings.
Its HTML goes into a sandboxed frame and is wrapped in a document whose
Content-Security-Policy allows nothing remote: a tracking pixel is a request
to a stranger's server the moment a mail is opened, and a mail client that
blocks remote content by default is the behaviour to copy.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import reflex as rx
from appkit_commons.registry import service_registry
from pydantic import BaseModel, ConfigDict

from mailarc_core import ArchiveReader
from mailarc_core.archive.model import MessageLabel, MessageSummary
from mailarc_core.mail import render_message
from mailarc_core.mail.model import (
    EmailAddress,
    LabelKind,
    ParsedAttachment,
    RenderedMessage,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
"""How many rows one read brings in. The list scrolls; "more" appends a page."""

RAW_LIMIT = 256 * 1024
"""How much of an original the viewer shows, in characters.

A review wants the headers and the first part of the body; the megabytes of
base64 behind them are on disk either way and nothing a human reads.
"""

NO_SUBJECT = "(no subject)"

YESTERDAY = "Yesterday"

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

FRAME_STYLE = (
    "body{margin:0;padding:16px;font:14px/1.5 -apple-system,BlinkMacSystemFont,"
    "'Segoe UI',Helvetica,Arial,sans-serif;color:#1a1a1a;background:#fff;"
    "word-wrap:break-word}img{max-width:100%}"
)
"""A quiet default look for mails that bring no styling of their own."""


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


class MessageRow(BaseModel):
    """One list row — everything already a string the browser can print.

    Frozen, because a row is a reading: selecting one hands its id back to
    the state, and the original is read by the digest it carries.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    sender: str
    subject: str
    preview: str
    date_label: str
    has_attachments: bool = False
    eml_sha256: str = ""
    labels: list[LabelChip] = []

    @classmethod
    def from_summary(cls, summary: MessageSummary, now: datetime) -> MessageRow:
        return cls(
            id=summary.id,
            sender=summary.sender_name or summary.sender_address,
            subject=summary.subject or NO_SUBJECT,
            preview=summary.preview,
            date_label=date_label(summary.sent_at, now),
            has_attachments=summary.has_attachments,
            eml_sha256=summary.eml_sha256 or "",
            labels=[LabelChip.from_label(one) for one in summary.labels],
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


_NO_VIEW = MessageView()
"""Nothing chosen yet. A sentinel keeps every component free of `None`."""


def archive_reader() -> ArchiveReader:
    """The reader the composition root published. Call inside a method only."""
    try:
        return service_registry().get(ArchiveReader)
    except KeyError as error:
        raise RuntimeError(
            "No archive reader is registered — did app.composition run?"
        ) from error


class MessageReviewState(rx.State):
    """The review page: the list on the left, the chosen original on the right."""

    messages: list[MessageRow] = []
    total: int = 0
    selected_id: str = ""
    tab: str = TAB_MESSAGE
    view: MessageView = _NO_VIEW
    remote_allowed: bool = False
    """Whether this selection may fetch remote content — once, or by trust."""
    raw: str = ""
    raw_truncated: bool = False
    loading: bool = False
    loading_raw: bool = False
    error: str = ""
    message_note: str = ""
    """One quiet sentence about the current message — never a page error."""

    @rx.var
    def has_messages(self) -> bool:
        return len(self.messages) > 0

    @rx.var
    def has_more(self) -> bool:
        return len(self.messages) < self.total

    @rx.var
    def has_selection(self) -> bool:
        return self.selected_id != ""

    @rx.var
    def has_html_body(self) -> bool:
        return self.view.body_html != ""

    @rx.var
    def frame_html(self) -> str:
        """The document the sandbox loads, framed under the current policy."""
        if self.view.body_html == "":
            return ""
        return frame_document(self.view.body_html, allow_remote=self.remote_allowed)

    @rx.var
    def remote_blocked(self) -> bool:
        """Whether the bar asking about remote content should be up."""
        return self.view.remote_references > 0 and not self.remote_allowed

    @rx.var
    def remote_notice(self) -> str:
        count = self.view.remote_references
        thing = "remote reference" if count == 1 else "remote references"
        return f"This message wants to load {count} {thing} — blocked."

    @rx.var
    def has_attachments(self) -> bool:
        return len(self.view.attachments) > 0

    @rx.var
    def has_cc(self) -> bool:
        return self.view.cc != ""

    @rx.var
    def count_label(self) -> str:
        if self.total == 0:
            return "No messages"
        shown = len(self.messages)
        return f"{shown} of {self.total}" if shown < self.total else f"{self.total}"

    @rx.event
    async def load(self) -> None:
        """Start over: the first page and the count. The page's ``on_load``."""
        self.error = ""
        self.loading = True
        try:
            reader = archive_reader()
            summaries, self.total = await asyncio.gather(
                asyncio.to_thread(reader.list_messages, limit=PAGE_SIZE, offset=0),
                asyncio.to_thread(reader.count_messages),
            )
            self.messages = self._rows(summaries)
            if self.selected_id not in {row.id for row in self.messages}:
                self._clear_selection()
        except Exception as error:
            logger.exception("Could not list the archive")
            self.error = str(error) or type(error).__name__
        finally:
            self.loading = False

    @rx.event
    async def load_more(self) -> None:
        """Append the next page; the list keeps what it has."""
        if self.loading or not self.has_more:
            return
        self.loading = True
        try:
            summaries = await asyncio.to_thread(
                archive_reader().list_messages,
                limit=PAGE_SIZE,
                offset=len(self.messages),
            )
            self.messages = [*self.messages, *self._rows(summaries)]
        except Exception as error:
            logger.exception("Could not list more of the archive")
            self.error = str(error) or type(error).__name__
        finally:
            self.loading = False

    @rx.event
    def show_tab(self, value: str) -> None:
        """Which of the two views is up. The choice outlives the selection."""
        self.tab = value if value in (TAB_MESSAGE, TAB_SOURCE) else TAB_MESSAGE

    @rx.event
    async def select(self, message_id: str) -> None:
        """Show one message — readable, and as it came off the wire.

        Both views come out of one read of the original; the parse runs off
        the event loop beside it, because a big HTML mail is real work.
        """
        row = next((one for one in self.messages if one.id == message_id), None)
        if row is None:
            return
        self.selected_id = message_id
        self._clear_views()
        if not row.eml_sha256:
            self._explain("No original was stored for this message.")
            return
        self.loading_raw = True
        try:
            data = await asyncio.to_thread(archive_reader().raw_message, row.eml_sha256)
            if data is None:
                self._explain("The original of this message is missing from the store.")
                return
            self.raw, self.raw_truncated = decode_raw(data)
            self.view = MessageView.from_rendered(
                await asyncio.to_thread(render_message, data)
            )
            if self.view.remote_references > 0 and self.view.sender_address:
                self.remote_allowed = await asyncio.to_thread(
                    archive_reader().remote_content_trusted, self.view.sender_address
                )
        except Exception as error:
            logger.exception("Could not read message %s", message_id)
            self._explain(f"Could not read the original: {error}")
        finally:
            self.loading_raw = False

    @rx.event
    def allow_remote_once(self) -> None:
        """Load the remote content of this one message, this one time."""
        self.remote_allowed = True

    @rx.event
    async def allow_remote_for_sender(self) -> None:
        """Load it now and from this sender always — recorded on the address."""
        address = self.view.sender_address
        if not address:
            return
        try:
            recorded = await asyncio.to_thread(
                archive_reader().trust_remote_content, address
            )
        except Exception:
            logger.exception("Could not record the trust decision for %s", address)
            recorded = False
        if not recorded:
            self.message_note = "The decision could not be stored; allowed once."
        self.remote_allowed = True

    def _explain(self, sentence: str) -> None:
        """Put one sentence where both views would show the message."""
        self.raw = sentence
        self.view = MessageView(body_text=sentence)

    def _clear_views(self) -> None:
        self.raw = ""
        self.raw_truncated = False
        self.view = _NO_VIEW
        self.remote_allowed = False
        self.message_note = ""

    def _rows(self, summaries: list[MessageSummary]) -> list[MessageRow]:
        now = datetime.now(UTC)
        return [MessageRow.from_summary(one, now) for one in summaries]

    def _clear_selection(self) -> None:
        self.selected_id = ""
        self._clear_views()


def date_label(sent_at: datetime | None, now: datetime) -> str:
    """The short date a list row shows, the way a mail client does it.

    Today is a time, yesterday is a word, anything older is a date. Both
    instants are moved to the local zone first, because "today" is a local
    notion and the archive stores UTC — and that move is what
    :func:`_local` has to survive.
    """
    local = _local(sent_at)
    if local is None:
        return ""
    today = now.astimezone().date()
    if local.date() == today:
        return local.strftime("%H:%M")
    if local.date() == today - timedelta(days=1):
        return YESTERDAY
    return local.strftime("%d.%m.%y")


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

    ``None`` covers two cases a row renders the same way: no date at all, and a
    date the local zone cannot express. The second is not hypothetical — a
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
