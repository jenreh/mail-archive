"""One message, one id — no matter which account or which run found it.

The contract this file exists for: importing the same mailbox twice creates
zero new nodes and zero new edges. The same mail reaching two of the user's
accounts is *one* ``Message`` node with two ``ARCHIVED_FROM`` edges, and that
only works if the id comes from the message rather than from the provider.

RFC 5322 says a Message-ID is globally unique, so it is the id whenever there
is one. Where a sender omitted it — spam, some mailing-list gateways, a few
scanner appliances — a content hash stands in. The hash inputs are picked to
be the fields a relay does not rewrite.
"""

import hashlib
from datetime import datetime

from mailarc_core.mail.model import EmailAddress

SHA256_PREFIX = "sha256:"
"""Marks an id we computed rather than one the sender supplied."""

_FIELD_SEPARATOR = "|"


def normalise_message_id(raw: str | None) -> str | None:
    """Strip the angle brackets and lowercase the domain half.

    ``<ABC.123@Example.COM>`` becomes ``ABC.123@example.com``. The local part
    keeps its case because RFC 5322 says it is significant there — unlike in
    an address, where every real mail system disagrees.

    An empty or whitespace-only header is the same as no header: ``None``.
    """
    if raw is None:
        return None
    candidate = raw.strip().strip("<>").strip()
    if not candidate:
        return None
    local, separator, domain = candidate.rpartition("@")
    if not separator:
        return candidate
    return f"{local}@{domain.lower()}"


def canonical_id(
    *,
    rfc_message_id: str | None,
    sent_at: datetime | None,
    sender: EmailAddress | None,
    subject: str,
    body_bytes: bytes,
) -> str:
    """The graph key for one message.

    The normalised Message-ID when there is one, otherwise
    ``sha256:`` over ``sent_at | from | subject | sha256(body)``.

    ``body_bytes`` is the *decoded* body, not the raw MIME part: a relay that
    re-encodes quoted-printable as base64 changes the bytes on the wire but
    not the text, and the id must survive that.
    """
    normalised = normalise_message_id(rfc_message_id)
    if normalised:
        return normalised

    fields = (
        sent_at.isoformat() if sent_at else "",
        sender.address if sender else "",
        subject,
        hashlib.sha256(body_bytes).hexdigest(),
    )
    digest = hashlib.sha256(_FIELD_SEPARATOR.join(fields).encode("utf-8")).hexdigest()
    return f"{SHA256_PREFIX}{digest}"
