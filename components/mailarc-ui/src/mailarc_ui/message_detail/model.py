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
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from appkit_commons.registry import service_registry
from pydantic import BaseModel, ConfigDict

from mailarc_analytics import GroupMembershipRow, TopicMembershipRow
from mailarc_core import ArchiveReader
from mailarc_core.archive.model import MessageLabel, TagSummary
from mailarc_core.mail.model import (
    EmailAddress,
    LabelKind,
    ParsedAttachment,
    RenderedMessage,
)
from mailarc_ui.message_detail.filetype import file_facts

logger = logging.getLogger(__name__)

RAW_LIMIT = 256 * 1024
"""How much of an original the viewer shows, in characters.

A reader wants the headers and the first part of the body; the megabytes of
base64 behind them are on disk either way and nothing a human reads.
"""

NO_SUBJECT = "(no subject)"
NO_FILENAME = "(unnamed)"
"""What a mail with no subject, and a part with no name, are called.

The filename is shared rather than written twice: the strip lists the file
and the panel that opens over it is titled the same way, and two spellings
of "this part had no name" would be two different files to a reader.
"""

TAB_MESSAGE = "message"
TAB_SOURCE = "source"
TAB_GRAPH = "graph"
"""The three ways of looking at one mail; the tab bar's values.

The third is not always there: it appears when a reader presses one of the
header's chips and stands for that topic, tag or group rather than for the
message — which is why it sits after the other two and carries the chip's own
name instead of a fixed one.
"""

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

KEYWORDS_ON_A_CHIP = 3
"""How many of a nameless topic's words fit on a chip before it stops being a
name and becomes a sentence."""

UNNAMED_TOPIC = "Topic"
UNNAMED_GROUP = "Group"
"""What a chip says when the archive has no name for the thing behind it.

Deliberately not the digest ``mailarc_ui.insights.model.short_key`` prints. A
listing shows many topics at once and has to tell two nameless ones apart; a
header shows the *one* topic this message is in, and eight hex characters buy
nothing there that a plain word does not.
"""


class ChipKind(StrEnum):
    """What the header can root the graph tab at.

    Five things and two shapes. Three of them are what the message *belongs
    to* and the header prints them as chips; the other two are the message
    itself and one address on it, which are already on the header as a subject
    and a name and want a glyph beside them rather than a pill of their own.
    Both shapes end up calling the same handler with one of these values.

    The values are :class:`~mailarc_analytics.NodeKind`'s own wherever there is
    one, because they cross into a read as the thing to root a picture at —
    and into the query string of the link out to the explorer. ``GROUP`` is the
    exception: it has no node kind and no view, see :data:`DRAWABLE_CHIPS`.
    """

    TOPIC = "topic"
    TAG = "tag"
    GROUP = "group"
    MESSAGE = "message"
    ADDRESS = "address"


CHIP_KINDS: tuple[ChipKind, ...] = (ChipKind.TOPIC, ChipKind.TAG, ChipKind.GROUP)
"""The kinds the header prints as chips, in the order it prints them.

The topic first because it is what the message is *about*, the tags next
because a person put them there, the group last because it is a fact about the
addressing rather than about the mail. The other two kinds are not chips: a
message and an address are already named on the header.
"""

CHIP_ICONS: Mapping[ChipKind, str] = MappingProxyType(
    {
        ChipKind.TOPIC: "layers",
        ChipKind.TAG: "tag",
        ChipKind.GROUP: "users",
    }
)
"""The glyph each kind of chip wears — :data:`CHIP_KINDS` and no more, which a
test checks. A mapping rather than a field on the value object, so a chip
carries what it *is* and this decides what that looks like."""

GRAPH_GLYPH = "share-2"
"""The one glyph that means "open this in the graph".

Worn by the icon beside the subject and beside every address, and the same
glyph the third tab carries. Not the kind's own icon: a person glyph next to a
person's name reads as decoration, and what these say is not *what this is* but
*what pressing it does*.
"""

DRAWABLE_CHIPS = frozenset(
    {ChipKind.TOPIC, ChipKind.TAG, ChipKind.MESSAGE, ChipKind.ADDRESS}
)
"""Which of the five have a graph behind them.

Four rooted views of the explorer:
:meth:`~mailarc_analytics.queries.graphs.GraphReader.topic`, ``.tag``,
``.message`` and ``.address``. A recurring group is a node in the graph but no
view is rooted at one and no reader answers for it, so its chip is shown and
not pressable — naming the group a message went to is worth something on its
own, and a chip that opened an empty canvas would be worth less than nothing.
"""


class AddressRow(BaseModel):
    """One person on the header — what to print, and what to root a graph at.

    Two fields and not one string, which is the change the glyphs paid for: a
    ``To`` line is a list of people and each of them has a picture of their
    own, so the line cannot be one joined label any more.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    """``Name <address>``, or the address alone — what the line prints."""

    address: str
    """The bare address the ``person`` view is rooted at."""


class MessageChip(BaseModel):
    """One thing the open message belongs to, as the header offers it.

    ``drawable`` is stored rather than derived because what reads it is an
    ``rx.cond`` in the browser: a property on the model is a Python call and
    never crosses. :func:`chips_for` is the one place that decides it.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    """A :class:`ChipKind` value — plain text, for the same reason
    :attr:`~mailarc_ui.graph.model.NodeCard.kind` is."""

    chip_id: str
    """What the graph is rooted at when the chip is pressed. Not ``id`` —
    that is a prop on every Reflex component."""

    label: str
    icon: str
    drawable: bool = False


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
    """One file on the message, as the header strip lists it.

    :attr:`kind_label` and :attr:`icon` are stored rather than derived from
    :attr:`content_type` where they are drawn, for the reason
    :attr:`MessageChip.drawable` is: what reads them is markup in the browser,
    and a classification is a Python call that never crosses. Deciding once,
    here, is also what keeps the card and the panel that opens over it saying
    the same thing about one file.
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    content_type: str
    size_label: str

    kind_label: str = ""
    """What the file is, in the shortest true form — ``PDF``, ``XLSX``,
    ``Spreadsheet``. See
    :func:`~mailarc_ui.message_detail.filetype.type_label`."""

    icon: str = "file"
    """The family's glyph."""

    @classmethod
    def from_attachment(cls, attachment: ParsedAttachment) -> AttachmentRow:
        """One card on the strip, from one parsed file.

        Classified once. This asked
        :func:`~mailarc_ui.message_detail.filetype.file_kind` four times per
        card — twice inside :func:`~mailarc_ui.message_detail.filetype.type_label`,
        once more for its fallback word and once for the glyph — which is four
        chances for one card to say two things about one file.
        """
        facts = file_facts(attachment.content_type, attachment.filename)
        return cls(
            filename=attachment.filename or NO_FILENAME,
            content_type=attachment.content_type,
            size_label=size_label(attachment.size),
            kind_label=facts.label,
            icon=facts.icon,
        )


class MessageView(BaseModel):
    """One message the way the readable tab shows it — all strings.

    ``body_html`` is the whole frame document, CSP and base style included,
    ready for an ``srcdoc``; empty when the mail has no HTML and the text
    body is what shows.
    """

    model_config = ConfigDict(frozen=True)

    subject: str = ""
    sender_name: str = ""
    """What the header prints in bold: the display name, or the address.

    Kept apart from :attr:`sender_address` rather than joined into one
    ``Name <address>`` label, because the header prints the two in different
    weights and colours the way a mail client does.
    """
    sender_address: str = ""
    """The bare address — the key a trust decision is recorded under."""
    recipients: list[AddressRow] = []
    """The ``To`` line. Not named ``to`` — that is a method on every reflex Var."""
    cc: list[AddressRow] = []
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
            sender_name=sender_label(rendered.sender),
            sender_address=rendered.sender.address if rendered.sender else "",
            recipients=address_rows(rendered.to),
            cc=address_rows(rendered.cc),
            date=long_date_label(rendered.sent_at),
            body_html=rendered.body_html or "",
            body_text=rendered.body_text,
            remote_references=rendered.remote_references,
            attachments=[
                AttachmentRow.from_attachment(one) for one in rendered.attachments
            ],
        )


def topic_chip(row: TopicMembershipRow) -> MessageChip:
    """The topic a message is in, named the way a chip can hold it.

    Its subject when its members had one, its words when they did not, and
    :data:`UNNAMED_TOPIC` when the clustering found neither — the same three
    steps :func:`mailarc_ui.search.model.topic_label` takes for a section
    heading, stopping one short of the digest for the reason given there.
    """
    return _chip(
        ChipKind.TOPIC,
        row.topic_id,
        row.label or " · ".join(row.keywords[:KEYWORDS_ON_A_CHIP]) or UNNAMED_TOPIC,
    )


def tag_chip(tag: TagSummary) -> MessageChip:
    """A tag a person put on the message, under the name they gave it."""
    return _chip(ChipKind.TAG, tag.id, tag.name or tag.id)


def group_chip(row: GroupMembershipRow) -> MessageChip:
    """``5 people`` — a recurring set of correspondents has a size, not a name."""
    people = "person" if row.size == 1 else "people"
    label = f"{row.size} {people}" if row.size else UNNAMED_GROUP
    return _chip(ChipKind.GROUP, row.group_id, label)


def _chip(kind: ChipKind, chip_id: str, label: str) -> MessageChip:
    return MessageChip(
        kind=kind.value,
        chip_id=chip_id,
        label=label,
        icon=CHIP_ICONS[kind],
        drawable=kind in DRAWABLE_CHIPS,
    )


def chips_for(
    topic: TopicMembershipRow | None,
    tags: Sequence[TagSummary],
    group: GroupMembershipRow | None,
) -> list[MessageChip]:
    """Everything the open message belongs to, in the order the header reads.

    In :data:`CHIP_KINDS` order. Anything absent is simply not a chip: a
    message in no topic, or an archive nobody has rebuilt, shows the chips it
    does have and no placeholder for the rest.
    """
    found = [] if topic is None else [topic_chip(topic)]
    found += [tag_chip(one) for one in tags]
    if group is not None:
        found.append(group_chip(group))
    return found


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


def sender_label(address: EmailAddress | None) -> str:
    """The name a header sets in bold — the display name, or the address.

    Never both: the address follows in its own grey chunk, and a sender who
    gave no name has only the one thing to print.
    """
    if address is None:
        return ""
    return address.display_name or address.address


def address_suffix(name: str, address: str) -> str:
    """The grey ``<address>`` that follows the sender's name, or nothing.

    Nothing when the name already *is* the address:
    ``shop@example.com <shop@example.com>`` is what the naive version of that
    line prints.
    """
    if not address or name == address:
        return ""
    return f"<{address}>"


def address_rows(addresses: Sequence[EmailAddress]) -> list[AddressRow]:
    """One header line's worth of people, each with its own root."""
    return [
        AddressRow(label=address_label(one), address=one.address) for one in addresses
    ]


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


FALLBACK_CHARSET = "cp1252"
"""What bytes are read as when nothing else worked.

Windows-1252 rather than latin-1, and they are not the same: the range 0x80 to
0x9F is unassigned in latin-1 and is where 1252 keeps the curly quotes, the en
dash and the euro sign — the characters a German mail program actually puts in
a contact card. Read as latin-1 they come out as control codes.

Last, and never first: it decodes almost anything without complaining, which
makes it a good floor and a terrible guess.
"""


def decode_raw(
    data: bytes, limit: int = RAW_LIMIT, charset: str = ""
) -> tuple[str, bool]:
    """Bytes to text for the viewer, and whether the cut was applied.

    Lenient on purpose — a replaced character is a fact about the message, a
    decode error would hide the whole of it. The cut lands after decoding so
    a multi-byte character is never split.

    Three attempts, and the order is the interesting part.

    **UTF-8 first, even when the part said otherwise.** UTF-8 validates
    itself: a sequence of bytes that is not UTF-8 almost never decodes as UTF-8
    by accident, so a successful strict decode is close to proof. The reverse is
    not true — latin-1 accepts *every* byte, so a UTF-8 file that a sender
    labelled ``iso-8859-1`` would decode without error into ``GeschÃ¤ftsleitung``
    and nothing downstream would ever notice. Trusting the label first is how
    that mojibake gets archived.

    **Then what the sender said**, which is the right answer for the genuinely
    single-byte files and is a statement of fact rather than a guess.

    **Then :data:`FALLBACK_CHARSET` with replacement**, so that there is always
    a string. That is the whole of the failure handling.
    """
    text = _decoded(data, charset)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _decoded(data: bytes, charset: str) -> str:
    """The bytes as text, down the ladder :func:`decode_raw` describes."""
    for candidate in ("utf-8", charset.strip().lower()):
        if not candidate:
            continue
        try:
            return data.decode(candidate)
        except UnicodeDecodeError, LookupError:
            logger.debug("Attachment is not %s", candidate)
    return data.decode(FALLBACK_CHARSET, errors="replace")
