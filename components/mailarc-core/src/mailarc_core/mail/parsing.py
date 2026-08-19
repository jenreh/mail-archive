"""RFC 5322 bytes to a :class:`ParsedMessage` — the only place mail is read.

Everything downstream trusts that this file already did the work: the graph
writer stores fields, the analyses read them, nobody re-reads a header. So the
five analysis-bearing fields are computed *here*, at import time, not later:

``subject_norm``
    Reply prefixes and ticket tokens gone, lowercased, whitespace collapsed.
``participant_key``
    sha256 over the sorted set of everyone on the message.
``simhash``
    64-bit SimHash over word shingles of ``body_clean``.
``refs``
    Ticket tokens out of the subject and the body.
``body_clean``
    The body without quoted predecessors, sign-off and legal footer.

``body_clean`` is the load-bearing one. ``body_text`` keeps the full text for
full-text search; ``body_clean`` is what gets hashed and embedded. Without the
cleaning step every message carrying the same company footer hashes alike and
the template analysis returns noise.

The cleaning rules are deliberately blunt heuristics over German and English
conventions — a reply intro, a ``>`` quote, a ``--`` separator, a sign-off, a
legal boilerplate opener. They cut a little too much rather than too little:
a lost sentence costs recall, a kept footer costs correctness.

Nothing here does I/O and nothing here raises for a merely odd message. Mail
in the wild is odd. Only MIME that cannot be read at all becomes a
:class:`~mailarc_core.mail.errors.MailPermanentError`.
"""

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from email import policy
from email.errors import (
    MultipartInvariantViolationDefect,
    StartBoundaryNotFoundDefect,
)
from email.message import EmailMessage, Message, MIMEPart
from email.parser import BytesParser
from email.utils import getaddresses
from functools import lru_cache
from html.parser import HTMLParser

from mailarc_core.mail.config import MailConfig
from mailarc_core.mail.errors import MailPermanentError
from mailarc_core.mail.identity import canonical_id, normalise_message_id
from mailarc_core.mail.model import EmailAddress, ParsedAttachment, ParsedMessage

logger = logging.getLogger(__name__)

SIMHASH_BITS = 64

_TOKEN = re.compile(r"\w+")
_WHITESPACE = re.compile(r"\s+")

#: ``Re:``, ``AW:``, ``Fwd:``, ``FW:``, ``WG:`` — with the ``[2]`` counter
#: some clients add. Stripped repeatedly, because forwarded replies stack.
_SUBJECT_PREFIX = re.compile(r"^\s*(?:re|aw|fwd?|wg|antw)\s*(?:\[\d+\])?\s*:\s*", re.I)

#: ``PROJ-123``, bracketed or bare. Uppercase key only: that is the convention
#: every issue tracker follows, and lowercasing it would match ordinary words.
_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,8})\b")

#: ``#4711``. Two digits minimum, so ``#1`` in prose is not a ticket.
_HASH_REF = re.compile(r"(?<![\w#])#(\d{2,8})(?!\d)")

_EMPTY_BRACKETS = re.compile(r"[\[(]\s*[\])]")

#: Uppercase keys that look like tickets but are standards and encodings.
_NOT_TICKET_KEYS = frozenset(
    {
        "AES",
        "ASCII",
        "COVID",
        "DIN",
        "EN",
        "IEC",
        "ISBN",
        "ISO",
        "MD",
        "MP",
        "RFC",
        "RGB",
        "SHA",
        "UTF",
    }
)

#: Where a quoted predecessor starts. The first match wins, so the German and
#: English intros, the Outlook separator and the Outlook header block are all
#: tried against the same text.
_QUOTE_MARKERS = (
    re.compile(
        r"^[ \t]*am\b[\s\S]{0,300}?\bschrieb\b[\s\S]{0,200}?:[ \t]*$", re.I | re.M
    ),
    re.compile(
        r"^[ \t]*on\b[\s\S]{0,300}?\bwrote\b[\s\S]{0,200}?:[ \t]*$", re.I | re.M
    ),
    re.compile(
        r"^[ \t]*-{2,}\s*(?:original message|ursprüngliche nachricht|"
        r"forwarded message|weitergeleitete nachricht)\s*-{2,}[ \t]*$",
        re.I | re.M,
    ),
    re.compile(
        r"^[ \t]*von:[^\n]*\n(?:[^\n]*\n){0,3}?[ \t]*(?:gesendet|datum|an|betreff):",
        re.I | re.M,
    ),
    re.compile(
        r"^[ \t]*from:[^\n]*\n(?:[^\n]*\n){0,3}?[ \t]*(?:sent|date|to|subject):",
        re.I | re.M,
    ),
)

_QUOTED_LINE = re.compile(r"^\s*>")

#: ``Grüße``, and the ways a client that lost the umlauts spells it.
_GRUESSE = r"gr(?:ü|ue|u)(?:ß|ss|s)e"

#: Where the message stops being the message: the RFC 3676 separator, a
#: sign-off, or a horizontal rule drawn out of punctuation.
_SIGNATURE_MARKERS = (
    re.compile(r"^--\s*$", re.M),
    re.compile(r"^[ \t]*[-_=*~]{6,}[ \t]*$", re.M),
    re.compile(
        r"^[ \t]*(?:"
        rf"mit (?:freundlichen|besten|herzlichen|lieben) {_GRUESSE}n|"
        rf"mit freundlichem gru(?:ß|ss)|"
        rf"(?:freundliche|viele|beste|liebe|herzliche|sonnige) {_GRUESSE}|"
        r"best regards|kind regards|warm regards|best wishes|"
        r"sincerely(?: yours)?|regards|cheers|"
        r"mfg|vg|lg|blg"
        r")[ \t]*[,.!]?[ \t]*$",
        re.I | re.M,
    ),
)

#: Boilerplate openers. Every one of them is a phrase that does not occur in a
#: sentence a human wrote to another human, which is what keeps the cut safe.
_DISCLAIMER_MARKERS = (
    re.compile(
        r"^[^\n]{0,120}?\b(?:vertraulichkeitshinweis|confidentiality notice|"
        r"sitz der gesellschaft|registergericht|handelsregister|amtsgericht|"
        r"ust-?idnr|umsatzsteuer-identifikationsnummer|hrb\s*\d|"
        r"please consider the environment|bitte denken sie an die umwelt)\b",
        re.I | re.M,
    ),
    re.compile(
        r"^[^\n]{0,120}?\bdiese\s+e-?mail\b[^\n]{0,200}?"
        r"\b(?:vertraulich|vertrauliche|irrtümlich|bestimmt)\b",
        re.I | re.M,
    ),
    re.compile(
        r"^[^\n]{0,120}?\bthis\s+e-?mail\b[^\n]{0,200}?"
        r"\b(?:confidential|privileged|intended recipient)\b",
        re.I | re.M,
    ),
    re.compile(
        r"^[^\n]{0,120}?\bthe information contained in this\b",
        re.I | re.M,
    ),
)

_FATAL_DEFECTS = (StartBoundaryNotFoundDefect, MultipartInvariantViolationDefect)


@lru_cache(maxsize=1)
def _default_config() -> MailConfig:
    """The settings a caller who did not care gets. Read once, not per message."""
    return MailConfig()


def parse_message(raw: bytes, *, config: MailConfig | None = None) -> ParsedMessage:
    """Turn one message's bytes into every field the ``Message`` node needs.

    Deterministic: the same bytes give the same :class:`ParsedMessage`,
    including ``canonical_id`` and ``simhash``. That is what makes a re-import
    a no-op instead of a duplicate.

    Raises :class:`~mailarc_core.mail.errors.MailPermanentError` when the MIME
    structure cannot be read. Retrying will not help, so the caller records the
    message as failed and moves on.
    """
    settings = config or _default_config()
    message = _parse_bytes(raw)

    subject = _header(message, "Subject")
    sender = _first(_addresses(message, "From"))
    to = _addresses(message, "To")
    cc = _addresses(message, "Cc")
    bcc = _addresses(message, "Bcc")
    sent_at = _sent_at(message)
    body_text = _body_text(message)
    body_clean = clean_body(body_text, settings)
    attachments = _attachments(message)
    references = _references(message)
    in_reply_to = normalise_message_id(_header(message, "In-Reply-To") or None)
    rfc_message_id = normalise_message_id(_header(message, "Message-ID") or None)

    return ParsedMessage(
        canonical_id=canonical_id(
            rfc_message_id=rfc_message_id,
            sent_at=sent_at,
            sender=sender,
            subject=subject,
            body_bytes=body_text.encode("utf-8"),
        ),
        rfc_message_id=rfc_message_id,
        subject=subject,
        subject_norm=normalise_subject(subject),
        sent_at=sent_at,
        sender=sender,
        to=to,
        cc=cc,
        bcc=bcc,
        body_text=body_text,
        body_clean=body_clean,
        simhash=simhash(body_clean, shingle_size=settings.shingle_size),
        participant_key=participant_key(
            (*((sender,) if sender else ()), *to, *cc, *bcc)
        ),
        refs=extract_refs(subject, body_text),
        size_bytes=len(raw),
        has_attachments=bool(attachments),
        eml_sha256=hashlib.sha256(raw).hexdigest(),
        in_reply_to=in_reply_to,
        references=references,
        attachments=attachments,
        thread_hint=references[0] if references else in_reply_to,
    )


def normalise_subject(subject: str) -> str:
    """Strip reply prefixes and ticket tokens, lowercase, collapse whitespace.

    ``AW: Re: [PROJ-123] Angebot Q3`` becomes ``angebot q3`` — the form A2
    compares on. Prefixes are stripped in a loop because a forwarded reply
    stacks them (``WG: AW: …``), and the ticket lives in ``refs`` anyway, so
    leaving it here would only split one project into two subjects.
    """
    text = subject
    while True:
        shorter = _SUBJECT_PREFIX.sub("", text, count=1)
        if shorter == text:
            break
        text = shorter
    text = _TICKET.sub(_blank_ticket, text)
    text = _HASH_REF.sub(" ", text)
    text = _EMPTY_BRACKETS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip().lower()


def participant_key(addresses: Iterable[EmailAddress]) -> str:
    """sha256 over the sorted set of everyone involved.

    A *set*, so a reply-all and its original share a key however the client
    reordered the recipients; *sorted*, so the key does not depend on header
    order. Empty when nobody could be read — a message with no addresses must
    not group with every other one.
    """
    unique = sorted({address.address for address in addresses if address.address})
    if not unique:
        return ""
    return hashlib.sha256("|".join(unique).encode("utf-8")).hexdigest()


def simhash(text: str, *, shingle_size: int = 3) -> int:
    """64-bit SimHash over word shingles — near-identical text, near bits.

    Shingles rather than single words because word frequency alone cannot tell
    a rewritten paragraph from a reordered one. Weighted by how often a shingle
    occurs, so a repeated boilerplate line pulls harder than a one-off phrase.

    Zero for empty text, which is also its own bucket: everything empty is
    trivially the same template and A3 discards it.
    """
    tokens = _TOKEN.findall(text.lower())
    if not tokens:
        return 0

    counts = Counter(_shingles(tokens, shingle_size))
    vector = [0] * SIMHASH_BITS
    for shingle, weight in counts.items():
        digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(SIMHASH_BITS):
            vector[bit] += weight if (value >> bit) & 1 else -weight

    result = 0
    for bit in range(SIMHASH_BITS):
        if vector[bit] > 0:
            result |= 1 << bit
    return result


def hamming_distance(left: int, right: int) -> int:
    """Differing bits between two SimHashes.

    How few is "few" belongs to A3, not here: the distance a one-word edit
    costs falls with body length, so the threshold is calibrated against
    real archives rather than fixed in this file.
    """
    return (left ^ right).bit_count()


def extract_refs(*texts: str) -> tuple[str, ...]:
    """Ticket and case tokens out of every text handed in, sorted and deduped.

    Read from the *full* body rather than the cleaned one: a top-posted "passt
    so" reply carries its ticket only in the quoted part, and losing it there
    would cost A2 its most reliable signal for the sake of a footer that
    almost never contains a ``#1234``.
    """
    found: set[str] = set()
    for text in texts:
        for key, number in _TICKET.findall(text):
            if key not in _NOT_TICKET_KEYS:
                found.add(f"{key}-{number}")
        found.update(f"#{number}" for number in _HASH_REF.findall(text))
    return tuple(sorted(found))


def clean_body(text: str, config: MailConfig) -> str:
    """The body with the parts nobody wrote for this message removed.

    Cuts at the earliest marker of any enabled kind, then drops what quoting
    left behind. One cut rather than several passes: a signature under a quote
    under a disclaimer is still just "everything after the first marker".
    """
    if not text:
        return ""

    markers: list[re.Pattern[str]] = []
    if config.strip_quotes:
        markers.extend(_QUOTE_MARKERS)
    if config.strip_signatures:
        markers.extend(_SIGNATURE_MARKERS)
        markers.extend(_DISCLAIMER_MARKERS)

    body = text[: _earliest_cut(text, markers)]
    if config.strip_quotes:
        body = "\n".join(
            line for line in body.splitlines() if not _QUOTED_LINE.match(line)
        )
    return _collapse(body)


def _blank_ticket(match: re.Match[str]) -> str:
    """Drop a ticket token from a subject — but not ``UTF-8`` or ``RFC-5322``."""
    return match.group(0) if match.group(1) in _NOT_TICKET_KEYS else " "


def _parse_bytes(raw: bytes) -> EmailMessage:
    """Read the bytes, or decide they are not a message at all."""
    if not raw.strip():
        raise MailPermanentError("empty message: there is nothing to parse")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        _reject_broken_mime(message)
    except MailPermanentError:
        raise
    except Exception as exc:
        raise MailPermanentError(
            f"unreadable message: {type(exc).__name__}: {exc}"
        ) from exc
    return message


def _reject_broken_mime(message: Message) -> None:
    """Refuse a multipart whose parts cannot be found.

    Most defects the stdlib records are cosmetic and the message still reads.
    These two are not: the declared boundary never appears, so the body is
    whatever happened to be between the headers and the end of the file. That
    is a message we would archive wrongly, and archiving it wrongly is worse
    than recording it as failed.
    """
    for part in message.walk():
        fatal = [d for d in part.defects if isinstance(d, _FATAL_DEFECTS)]
        if fatal:
            names = ", ".join(type(defect).__name__ for defect in fatal)
            raise MailPermanentError(f"broken MIME structure: {names}")


def _header(message: Message, name: str) -> str:
    """One header as text, empty when it is absent or undecodable."""
    try:
        value = message[name]
    except Exception:
        logger.debug("Header %s could not be read", name, exc_info=True)
        return ""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        logger.debug("Header %s could not be rendered", name, exc_info=True)
        return ""


def _addresses(message: Message, name: str) -> tuple[EmailAddress, ...]:
    """Every address on one header, in order, without the malformed ones.

    A header the provider mangled costs its addresses, not the message.
    """
    try:
        raw = [str(value) for value in message.get_all(name, [])]
    except Exception:
        logger.debug("Address header %s could not be read", name, exc_info=True)
        return ()

    seen: set[str] = set()
    found: list[EmailAddress] = []
    for display_name, address in getaddresses(raw):
        candidate = EmailAddress(address=address, display_name=display_name or None)
        if "@" not in candidate.address or candidate.address in seen:
            continue
        seen.add(candidate.address)
        found.append(candidate)
    return tuple(found)


def _first(addresses: Sequence[EmailAddress]) -> EmailAddress | None:
    """A ``From`` may list several senders; the graph edge wants one."""
    return addresses[0] if addresses else None


def _sent_at(message: Message) -> datetime | None:
    """The ``Date`` header as an aware datetime, or ``None`` if it is nonsense.

    RFC 5322 §3.3 gives ``-0000`` its own meaning — the time is UTC-based but
    the sender's own zone is deliberately withheld — and the stdlib renders
    that as a *naive* datetime. Mailing lists and anonymising relays use it
    routinely, so without this the archive would hold two shapes of
    ``sent_at`` in one range-indexed property and sort them against each
    other wrongly.
    """
    header = message["Date"]
    if header is None:
        return None
    value = getattr(header, "datetime", None)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    logger.debug("Date header could not be parsed: %r", str(header))
    return None


def _references(message: Message) -> tuple[str, ...]:
    """The ``References`` chain, normalised like every other Message-ID."""
    raw = _header(message, "References")
    if not raw:
        return ()
    normalised = (normalise_message_id(token) for token in raw.split())
    return tuple(token for token in normalised if token)


def _body_text(message: EmailMessage) -> str:
    """The readable text of the message, plain text for preference.

    HTML is a fallback, not an equal: a marketing mail's HTML carries layout
    that would drown the words in the SimHash. Stripping it to text loses
    nothing the analyses use.

    Line endings come out as ``\\n`` whatever the transport used. A relay that
    rewrites CRLF must not change ``body_clean`` — or the fallback identity —
    for a message that is otherwise word for word the same.
    """
    plain = _preferred_part(message, "plain")
    if plain is not None:
        return _normalise_newlines(_part_text(plain))

    html = _preferred_part(message, "html")
    if html is not None:
        return _html_to_text(_part_text(html))

    if not message.is_multipart():
        return _normalise_newlines(_part_text(message))
    return ""


def _normalise_newlines(text: str) -> str:
    """CRLF and bare CR down to LF, so every rule below sees one shape."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _preferred_part(message: EmailMessage, subtype: str) -> MIMEPart | None:
    """``get_body`` with its exceptions turned into "there is no such part"."""
    try:
        return message.get_body(preferencelist=(subtype,))
    except Exception:
        logger.debug("Could not select the %s body", subtype, exc_info=True)
        return None


def _part_text(part: MIMEPart) -> str:
    """Decoded text of one part; an unknown charset falls back to UTF-8."""
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return ""
        content = payload.decode("utf-8", errors="replace")
    return content if isinstance(content, str) else ""


def _attachments(message: EmailMessage) -> tuple[ParsedAttachment, ...]:
    """Every non-body part, hashed so the same file lands on one node."""
    try:
        parts = list(message.iter_attachments())
    except Exception:
        logger.debug("Attachments could not be enumerated", exc_info=True)
        return ()

    found: list[ParsedAttachment] = []
    for part in parts:
        data = _attachment_bytes(part)
        content_id = _header(part, "Content-ID")
        found.append(
            ParsedAttachment(
                filename=part.get_filename(),
                content_type=part.get_content_type(),
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                content_id=content_id.strip("<>") or None,
                inline=part.get_content_disposition() == "inline",
                payload=data,
            )
        )
    return tuple(found)


def _attachment_bytes(part: MIMEPart) -> bytes:
    """The bytes of one attachment, whatever shape the part is.

    ``get_payload(decode=True)`` returns ``None`` for every *container* part,
    and a forwarded mail — ``message/rfc822`` holding a multipart — is exactly
    that. Falling back to empty would hash all of them to ``sha256(b"")``:
    every forward in the archive would collapse onto one ``Attachment`` node
    wearing the content type of whichever arrived first, and the bytes would
    never reach the blob store.

    A ``message/*`` part is unwrapped rather than serialised, so what lands on
    disk is the forwarded message itself and not the MIME envelope that
    carried it — open the blob and you have the ``.eml``. Any other container
    is serialised as it stands, because there the envelope is the content.

    An empty result therefore means a genuinely empty attachment, and
    content-addressing it is then correct rather than a collision.
    """
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    try:
        nested = part.get_payload()
        if (
            part.get_content_maintype() == "message"
            and isinstance(nested, list)
            and len(nested) == 1
            and isinstance(nested[0], Message)
        ):
            return nested[0].as_bytes()
        return part.as_bytes()
    except Exception:
        logger.debug("Attachment part could not be serialised", exc_info=True)
        return b""


def _shingles(tokens: Sequence[str], size: int) -> Iterator[str]:
    """Overlapping runs of ``size`` words; single words when there are too few."""
    if size < 1 or len(tokens) < size:
        yield from tokens
        return
    for start in range(len(tokens) - size + 1):
        yield " ".join(tokens[start : start + size])


def _earliest_cut(text: str, markers: Iterable[re.Pattern[str]]) -> int:
    """Offset of the first line any marker matches, or the end of the text."""
    cut = len(text)
    for marker in markers:
        match = marker.search(text)
        if match is not None:
            cut = min(cut, text.rfind("\n", 0, match.start()) + 1)
    return cut


def _collapse(text: str) -> str:
    """Trailing spaces gone, runs of blank lines down to one, stripped."""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped and (not kept or not kept[-1]):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


class _TextExtractor(HTMLParser):
    """Just enough HTML to recover the words. Not a renderer.

    Block-level tags become newlines so paragraphs survive as paragraphs;
    ``script`` and ``style`` content is dropped rather than read as text.
    """

    _BLOCK = frozenset(
        {
            "br", "div", "h1", "h2", "h3", "h4", "h5", "h6",
            "li", "p", "table", "td", "tr", "ul", "ol",
        }
    )  # fmt: skip
    _SILENT = frozenset({"head", "script", "style", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],  # noqa: ARG002 - HTMLParser's signature
    ) -> None:
        if tag in self._SILENT:
            self._depth += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SILENT and self._depth:
            self._depth -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _html_to_text(html: str) -> str:
    """Tags out, entities resolved, blank runs collapsed."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:
        logger.debug("HTML body could not be parsed", exc_info=True)
    lines = (
        _WHITESPACE.sub(" ", line).strip() for line in extractor.text().split("\n")
    )
    return _collapse("\n".join(lines))
