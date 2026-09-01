"""One message the way a mail client shows it.

The parser reduces a message to what the graph stores and deliberately throws
the HTML away — layout would only drown the words. A human looking at one mail
wants the opposite: the sender's markup, the inline pictures, the files. This
module builds that :class:`~mailarc_core.mail.model.RenderedMessage` out of the
same bytes, leaning on :func:`~mailarc_core.mail.parsing.parse_message` for
everything it already gets right — addresses, date, the attachment walk — and
adding only the HTML body.

``cid:`` references are the one bit of rewriting. An HTML mail points at its
own inline images by ``Content-ID``; rendered anywhere but the original client
those links are dead. Swapping each for a data URI built from the attachment
keeps the picture without needing a server to fetch it from. Nothing else in
the markup is touched — sanitising is the viewer's job, and a sandboxed frame
does it better than a tag list would.
"""

import base64
import logging
import re
from email import policy
from email.message import MIMEPart
from email.parser import BytesParser

from mailarc_core.mail.config import MailConfig
from mailarc_core.mail.model import ParsedAttachment, RenderedMessage
from mailarc_core.mail.parsing import parse_message

logger = logging.getLogger(__name__)

INLINE_LIMIT = 2 * 1024 * 1024
"""The largest attachment worth inlining as a data URI, in bytes.

Past this a ``cid:`` link stays dead: a two-megabyte photo is already a lot of
base64 to ship to a browser, and a bigger one would not render for minutes.
"""

_CID = re.compile(r"""(?i)\bcid:([^"'\s>)]+)""")

_REMOTE = re.compile(
    r"""(?ix)
    (?:
        \b(?:src|srcset|poster|background)\s*=\s*["']?\s*   # fetched attributes
      | url\(\s*["']?\s*                                    # css url(...)
      | <link\b[^>]*\bhref\s*=\s*["']?\s*                  # stylesheets
    )
    (?:https?:)?//
    """
)
"""A reference the browser would fetch from elsewhere the moment it renders.

Attributes that *load* only — a clicked ``<a href>`` is the reader's decision
and is not counted. Protocol-relative ``//`` counts: it fetches too.
"""


def render_message(raw: bytes, *, config: MailConfig | None = None) -> RenderedMessage:
    """Bytes to what a mail client would put on screen. Never raises for
    a malformed body — what cannot be rendered is simply absent."""
    parsed = parse_message(raw, config=config)
    html = _html_body(raw)
    body_html = inline_cid_images(html, parsed.attachments) if html else None
    return RenderedMessage(
        subject=parsed.subject,
        sender=parsed.sender,
        to=parsed.to,
        cc=parsed.cc,
        sent_at=parsed.sent_at,
        body_html=body_html,
        body_text=parsed.body_text,
        attachments=parsed.attachments,
        remote_references=count_remote_references(body_html) if body_html else 0,
    )


def count_remote_references(html: str) -> int:
    """How many things this markup would fetch from another server.

    Counted after the ``cid:`` rewrite, so an inlined picture is not held
    against the mail — what remains is what a viewer must decide about.
    """
    return len(_REMOTE.findall(html))


def inline_cid_images(html: str, attachments: tuple[ParsedAttachment, ...]) -> str:
    """Replace every ``cid:`` reference with the attachment it names, inlined.

    Matched by ``Content-ID`` with the angle brackets gone, case-insensitively
    — clients disagree about the case and the brackets but mean the same part.
    A reference nothing answers is left as it was.
    """
    by_id = {
        one.content_id.lower(): one
        for one in attachments
        if one.content_id and one.payload and len(one.payload) <= INLINE_LIMIT
    }
    if not by_id:
        return html

    def replace(match: re.Match[str]) -> str:
        attachment = by_id.get(match.group(1).strip("<>").lower())
        if attachment is None:
            return match.group(0)
        encoded = base64.b64encode(attachment.payload).decode("ascii")
        return f"data:{attachment.content_type};base64,{encoded}"

    return _CID.sub(replace, html)


def _html_body(raw: bytes) -> str | None:
    """The HTML body if the message has one, decoded leniently."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        part = message.get_body(preferencelist=("html",))
    except Exception:
        logger.debug("Could not select the HTML body", exc_info=True)
        return None
    if part is None:
        return None
    return _part_text(part) or None


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
