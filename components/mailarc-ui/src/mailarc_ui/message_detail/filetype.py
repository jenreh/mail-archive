"""What kind of file an attachment is — its glyph, its name, and its preview.

One classification, read by three things that used to guess separately: the
card on the message header (which glyph, and what to call the file), the panel
that opens over it (which of four shapes to draw), and the search form's file
filter. Keeping it in one module is the point — a mail whose card says
*Spreadsheet* and whose panel says *no preview* is telling a reader two things
about one file, and only a single table can stop that.

No I/O and nothing from the rest of the package: a content type and a filename
in, an enum out. :mod:`mailarc_ui.message_detail.model` and
:mod:`mailarc_ui.message_detail.preview` both read it, which is also why it
imports neither of them. The one import is
:data:`~mailarc_core.mail.model.UNTYPED_CONTENT_TYPES` — which senders said
nothing is a fact about MIME that the archive needs too, and one copy of it
cannot drift from the other.

**The type decides and the name only breaks a tie.** A sender who says
``application/pdf`` is believed. A sender who says ``application/octet-stream``
— which several mail programs send for *every* attachment — has said nothing,
and only then is the filename's extension consulted. The other way round would
let a file called ``Rechnung.pdf`` override a sender who knew it was a zip.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType

from mailarc_core.mail.model import UNTYPED_CONTENT_TYPES


class FileKind(StrEnum):
    """The families a mail attachment falls into.

    More families than there are ways to draw one, deliberately. A ``.xlsx``
    and a ``.zip`` are both files a browser cannot render, and calling them
    both "no preview" is right — but on the header strip they are a
    spreadsheet and an archive, and a reader scanning six cards is reading the
    glyphs, not the names. What a file *is* and what can be *done* with it are
    two questions;
    :data:`~mailarc_ui.message_detail.preview.PREVIEW_OF_KIND` is the answer to
    the second in terms of the first.
    """

    IMAGE = "image"
    PDF = "pdf"
    WEB = "web"
    TEXT = "text"
    DATA = "data"
    TABLE = "table"
    MAIL = "mail"
    CALENDAR = "calendar"
    SPREADSHEET = "spreadsheet"
    DOCUMENT = "document"
    SLIDES = "slides"
    ARCHIVE = "archive"
    AUDIO = "audio"
    VIDEO = "video"
    OTHER = "other"


FILE_ICONS: Mapping[FileKind, str] = MappingProxyType(
    {
        FileKind.IMAGE: "file-image",
        FileKind.PDF: "file-text",
        FileKind.WEB: "file-code",
        FileKind.TEXT: "file-type",
        FileKind.DATA: "file-json",
        FileKind.TABLE: "file-spreadsheet",
        FileKind.MAIL: "mail",
        FileKind.CALENDAR: "calendar",
        FileKind.SPREADSHEET: "file-spreadsheet",
        FileKind.DOCUMENT: "file-text",
        FileKind.SLIDES: "presentation",
        FileKind.ARCHIVE: "file-archive",
        FileKind.AUDIO: "file-audio",
        FileKind.VIDEO: "file-video",
        FileKind.OTHER: "file",
    }
)
"""The glyph each family wears, from what lucide actually ships.

Two pairs share one: a PDF and a Word document are both ``file-text``, a CSV
and a spreadsheet are both ``file-spreadsheet``. That is not a gap to fill with
a near-miss glyph — lucide has no PDF mark, and the label beside the icon
already says *PDF* or *DOCX*. A glyph that is nearly right is worse than one
that is honestly a family.
"""

FILE_LABELS: Mapping[FileKind, str] = MappingProxyType(
    {
        FileKind.IMAGE: "Image",
        FileKind.PDF: "PDF",
        FileKind.WEB: "Web page",
        FileKind.TEXT: "Text",
        FileKind.DATA: "Data",
        FileKind.TABLE: "Table",
        FileKind.MAIL: "Email",
        FileKind.CALENDAR: "Calendar",
        FileKind.SPREADSHEET: "Spreadsheet",
        FileKind.DOCUMENT: "Document",
        FileKind.SLIDES: "Slides",
        FileKind.ARCHIVE: "Archive",
        FileKind.AUDIO: "Audio",
        FileKind.VIDEO: "Video",
        FileKind.OTHER: "File",
    }
)
"""What a family is called when nothing more concrete is known — the search
filter's options, and :func:`type_label`'s last resort."""

TYPE_KINDS: Mapping[str, FileKind] = MappingProxyType(
    {
        "application/gzip": FileKind.ARCHIVE,
        "application/ics": FileKind.CALENDAR,
        "application/javascript": FileKind.DATA,
        "application/json": FileKind.DATA,
        "application/msword": FileKind.DOCUMENT,
        "application/pdf": FileKind.PDF,
        "application/rtf": FileKind.DOCUMENT,
        "application/vnd.ms-excel": FileKind.SPREADSHEET,
        "application/vnd.ms-outlook": FileKind.MAIL,
        "application/vnd.ms-powerpoint": FileKind.SLIDES,
        "application/vnd.oasis.opendocument.presentation": FileKind.SLIDES,
        "application/vnd.oasis.opendocument.spreadsheet": FileKind.SPREADSHEET,
        "application/vnd.oasis.opendocument.text": FileKind.DOCUMENT,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
            FileKind.SLIDES
        ),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
            FileKind.SPREADSHEET
        ),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
            FileKind.DOCUMENT
        ),
        "application/vnd.rar": FileKind.ARCHIVE,
        "application/x-7z-compressed": FileKind.ARCHIVE,
        "application/x-ndjson": FileKind.DATA,
        "application/x-rar-compressed": FileKind.ARCHIVE,
        "application/x-tar": FileKind.ARCHIVE,
        "application/x-yaml": FileKind.DATA,
        "application/x-zip-compressed": FileKind.ARCHIVE,
        "application/xhtml+xml": FileKind.WEB,
        "application/xml": FileKind.DATA,
        "application/yaml": FileKind.DATA,
        "application/zip": FileKind.ARCHIVE,
        "message/delivery-status": FileKind.MAIL,
        "message/rfc822": FileKind.MAIL,
        "text/calendar": FileKind.CALENDAR,
        "text/csv": FileKind.TABLE,
        "text/html": FileKind.WEB,
        "text/rtf": FileKind.DOCUMENT,
        "text/tab-separated-values": FileKind.TABLE,
        "text/xml": FileKind.DATA,
    }
)
"""Content types a sender states exactly, and what each one is.

Consulted before :data:`TYPE_PREFIXES`, because every entry here is more
specific than the prefix that would otherwise catch it — ``text/csv`` is a
table before it is text, and ``text/rtf`` is a document.
"""

TYPE_PREFIXES: tuple[tuple[str, FileKind], ...] = (
    ("image/", FileKind.IMAGE),
    ("audio/", FileKind.AUDIO),
    ("video/", FileKind.VIDEO),
    ("text/", FileKind.TEXT),
)
"""The open-ended families. A tuple and not a mapping: these are tried in
order, and ``text/`` has to come last so the exact table's ``text/*`` entries
are not shadowed by it."""

EXTENSION_KINDS: Mapping[str, FileKind] = MappingProxyType(
    {
        ".7z": FileKind.ARCHIVE,
        ".aac": FileKind.AUDIO,
        ".avi": FileKind.VIDEO,
        ".avif": FileKind.IMAGE,
        ".bmp": FileKind.IMAGE,
        ".bz2": FileKind.ARCHIVE,
        ".cfg": FileKind.TEXT,
        ".csv": FileKind.TABLE,
        ".doc": FileKind.DOCUMENT,
        ".docx": FileKind.DOCUMENT,
        ".eml": FileKind.MAIL,
        ".flac": FileKind.AUDIO,
        ".gif": FileKind.IMAGE,
        ".gz": FileKind.ARCHIVE,
        ".heic": FileKind.IMAGE,
        ".htm": FileKind.WEB,
        ".html": FileKind.WEB,
        ".ics": FileKind.CALENDAR,
        ".ini": FileKind.TEXT,
        ".jpeg": FileKind.IMAGE,
        ".jpg": FileKind.IMAGE,
        ".json": FileKind.DATA,
        ".key": FileKind.SLIDES,
        ".log": FileKind.TEXT,
        ".m4a": FileKind.AUDIO,
        ".md": FileKind.TEXT,
        ".mkv": FileKind.VIDEO,
        ".mov": FileKind.VIDEO,
        ".mp3": FileKind.AUDIO,
        ".mp4": FileKind.VIDEO,
        ".msg": FileKind.MAIL,
        ".numbers": FileKind.SPREADSHEET,
        ".odp": FileKind.SLIDES,
        ".ods": FileKind.SPREADSHEET,
        ".odt": FileKind.DOCUMENT,
        ".ogg": FileKind.AUDIO,
        ".pages": FileKind.DOCUMENT,
        ".pdf": FileKind.PDF,
        ".png": FileKind.IMAGE,
        ".ppt": FileKind.SLIDES,
        ".pptx": FileKind.SLIDES,
        ".rar": FileKind.ARCHIVE,
        ".rtf": FileKind.DOCUMENT,
        ".svg": FileKind.IMAGE,
        ".tar": FileKind.ARCHIVE,
        ".tgz": FileKind.ARCHIVE,
        ".tif": FileKind.IMAGE,
        ".tiff": FileKind.IMAGE,
        ".tsv": FileKind.TABLE,
        ".txt": FileKind.TEXT,
        ".wav": FileKind.AUDIO,
        ".webm": FileKind.VIDEO,
        ".webp": FileKind.IMAGE,
        ".xls": FileKind.SPREADSHEET,
        ".xlsx": FileKind.SPREADSHEET,
        ".xml": FileKind.DATA,
        ".yaml": FileKind.DATA,
        ".yml": FileKind.DATA,
        ".zip": FileKind.ARCHIVE,
    }
)
"""The fallback for a sender who said nothing — see the module docstring."""

GENERIC_TYPES = UNTYPED_CONTENT_TYPES
"""Content types that are a shrug, and the only ones a filename overrules.

The archive's own list, under the name this module reads it by: it decides
which files the search picker offers **by name** rather than by type, and a
second copy here would put a file in one family on a card and another in the
filter. See :data:`~mailarc_core.mail.model.UNTYPED_CONTENT_TYPES`.
"""

DULL_SUBTYPES = frozenset({"plain"})
"""Subtypes that are short enough to read as a label and say nothing.

``text/plain`` would otherwise put ``PLAIN`` on an unnamed part, which names
the encoding rather than the file; the family word ``Text`` is what a reader
wanted. Only subtypes short enough to pass :data:`LABEL_LENGTH` belong here —
``octet-stream``, ``binary`` and ``rfc822`` are already excluded by it, and a
test keeps this set from collecting entries that do nothing.
"""

LABEL_LENGTH = 5
"""How many characters a label may take from an extension or a subtype.

``PDF``, ``XLSX`` and ``JPEG`` are names a person reads at a glance;
``vnd.openxmlformats-officedocument.spreadsheetml.sheet`` is not a label, and
neither is ``rfc822``. Past this the family word is the better answer.
"""


def file_kind(content_type: str, filename: str | None = None) -> FileKind:
    """Which family this file belongs to.

    Never raises and never returns ``None``: an unrecognised file is
    :attr:`FileKind.OTHER`, which is a real answer with its own glyph and its
    own word.
    """
    normalised = _normalise(content_type)
    found = _kind_of_type(normalised)
    if found is not FileKind.OTHER:
        return found
    if normalised not in GENERIC_TYPES:
        return FileKind.OTHER
    return EXTENSION_KINDS.get(_suffix(filename), FileKind.OTHER)


def type_label(content_type: str, filename: str | None = None) -> str:
    """What the card calls this file — ``PDF``, ``XLSX``, ``Spreadsheet``.

    The extension first, because it is what the sender named the file and what
    a reader would say out loud. Its own content type's subtype next, for a
    part that arrived without a name. The family word last, when neither is
    short enough to read as a label — see :data:`LABEL_LENGTH`.
    """
    extension = _suffix(filename).removeprefix(".")
    if _is_label(extension):
        return extension.upper()
    subtype = _normalise(content_type).rpartition("/")[2]
    if _is_label(subtype):
        return subtype.upper()
    return FILE_LABELS[file_kind(content_type, filename)]


def _is_label(candidate: str) -> bool:
    """Whether this fragment reads as a file type rather than as debris."""
    return (
        bool(candidate)
        and candidate.isalnum()
        and len(candidate) <= LABEL_LENGTH
        and candidate not in DULL_SUBTYPES
    )


def _normalise(content_type: str) -> str:
    """A content type reduced to what the tables are keyed by.

    Lowercased and stripped. Parameters are cut as well —
    :meth:`email.message.MIMEPart.get_content_type` never returns one, but this
    function is also handed types out of the graph, and one stored with a
    ``; charset=utf-8`` still on it should classify rather than fall to
    ``OTHER``.
    """
    return content_type.split(";", maxsplit=1)[0].strip().lower()


def _kind_of_type(normalised: str) -> FileKind:
    if normalised in TYPE_KINDS:
        return TYPE_KINDS[normalised]
    for prefix, kind in TYPE_PREFIXES:
        if normalised.startswith(prefix):
            return kind
    return FileKind.OTHER


def _suffix(filename: str | None) -> str:
    """The extension, lowercased, or ``""`` — never an exception.

    ``PurePosixPath`` on a name that is really a sentence (``Re: Fwd. Notes``)
    returns ``. notes``, which matches nothing in the table and is exactly the
    right outcome; nothing here has to defend against it.
    """
    return PurePosixPath(filename or "").suffix.lower()
