"""Google's JSON in, the domain's value objects out. Nothing else leaves.

The anti-corruption layer of §3.1, in one file. ``labelIds``, ``threadId``,
``resultSizeEstimate`` and ``emailAddress`` are Gmail's words for things
:mod:`mailarc_core.mail.model` already has names for, and they stop here — no
dictionary crosses out of this module, which is what keeps a Gmail field from
surfacing in the graph two layers away.

Pure functions: no I/O, no status codes, no credentials. The only error raised
is :class:`~mailarc_core.mail.errors.MailPermanentError`, and always for the
same reason — asking Gmail again for a record it just sent malformed returns
the same malformed record, so the message gets skipped and written down (§7.6).
"""

import base64
import logging
from collections.abc import Mapping
from typing import Any

from mailarc_core.mail.errors import MailPermanentError
from mailarc_core.mail.model import (
    AccountIdentity,
    EmailAddress,
    LabelInfo,
    LabelKind,
    MailProvider,
    MessagePage,
    MessageRef,
    RawMessage,
    SyncCursor,
    SyncCursorKind,
)

logger = logging.getLogger(__name__)

_LABEL_KINDS = {"system": LabelKind.SYSTEM, "user": LabelKind.USER}
"""Gmail's ``type`` field, which has exactly these two values.

``FOLDER`` is IMAP's and stays unused. Anything Gmail adds later reads as a
user label, because that is what a new label kind would almost certainly be.
"""


def account_identity(payload: Mapping[str, Any]) -> AccountIdentity:
    """Whose mailbox the token just opened, from a ``users.getProfile`` reply.

    The address doubles as ``provider_account_id``: Gmail has no other handle
    for an account — ``users/me`` resolves to exactly this — and inventing one
    from the ``historyId`` would tie an account's identity to how far it has
    been read.
    """
    address = _required(payload, "emailAddress")
    return AccountIdentity(
        provider=MailProvider.GMAIL,
        address=EmailAddress(address=address),
        provider_account_id=address,
    )


def labels(payload: Mapping[str, Any]) -> tuple[LabelInfo, ...]:
    """Every label of the account, from a ``labels.list`` reply."""
    entries = payload.get("labels") or ()
    return tuple(label_info(one) for one in entries)


def label_info(payload: Mapping[str, Any]) -> LabelInfo:
    """One label resource.

    ``name`` falls back to the id: a label with no name would otherwise become
    a nameless node, and an id on the graph is at least something a human can
    look up.
    """
    identifier = _required(payload, "id")
    return LabelInfo(
        provider_label_id=identifier,
        name=str(payload.get("name") or identifier),
        kind=_LABEL_KINDS.get(str(payload.get("type") or "").lower(), LabelKind.USER),
        message_count=_count(payload.get("messagesTotal")),
    )


def message_page(payload: Mapping[str, Any]) -> MessagePage:
    """One ``messages.list`` reply, with Gmail's page token sealed in a cursor.

    The token goes into :class:`~mailarc_core.mail.model.SyncCursor` and the
    engine never looks inside it — that is the whole point of the cursor being
    opaque. No next token means the walk is over.
    """
    entries = payload.get("messages") or ()
    token = payload.get("nextPageToken")
    return MessagePage(
        refs=tuple(message_ref(one) for one in entries),
        next_cursor=(
            SyncCursor(
                provider=MailProvider.GMAIL,
                token=str(token),
                kind=SyncCursorKind.FULL,
            )
            if token
            else None
        ),
        estimated_total=_count(payload.get("resultSizeEstimate")),
    )


def message_ref(payload: Mapping[str, Any]) -> MessageRef:
    """One message's metadata, from a listing entry or from a full message.

    Both shapes come through here, and they are not equally rich: a listing
    entry carries an id and a thread id, a fetched message also carries its
    labels and its size. Everything absent is simply absent.
    """
    return MessageRef(
        provider_message_id=_required(payload, "id"),
        provider_thread_id=str(payload["threadId"])
        if payload.get("threadId")
        else None,
        labels=tuple(str(one) for one in payload.get("labelIds") or ()),
        size_estimate=_count(payload.get("sizeEstimate")),
    )


def raw_message(payload: Mapping[str, Any]) -> RawMessage:
    """One ``format=raw`` message: its bytes and the metadata beside them.

    The reference is rebuilt from *this* reply rather than carried over from
    the listing, because this is the richer one — it names the labels, and the
    labels are what the archive hangs on the message.
    """
    return RawMessage(ref=message_ref(payload), raw=_decode(_required(payload, "raw")))


def _decode(encoded: str) -> bytes:
    """Gmail's ``raw`` field, which is base64**URL** and usually unpadded.

    Base64url puts ``-`` and ``_`` where standard base64 has ``+`` and ``/``.
    :func:`base64.b64decode` does not reject the other alphabet — it *discards*
    the characters it does not know — so decoding with the wrong one works
    perfectly until the first message whose encoding happens to contain either,
    which is roughly one in sixty, and then silently produces wrong bytes. The
    padding is Google's to omit and ours to put back.
    """
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except ValueError as error:
        raise MailPermanentError(
            f"Gmail's raw field is not base64url: {error}"
        ) from error


def _required(payload: Mapping[str, Any], key: str) -> str:
    """A string field the rest of the mapping cannot do without."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MailPermanentError(f"Gmail's reply carries no {key}")
    return value


def _count(value: object) -> int | None:
    """A Gmail counter as an ``int``, or ``None`` when it is neither.

    Counters arrive as JSON numbers, as strings, or not at all, depending on
    the field and the endpoint. None of them is worth failing a whole page
    over: an estimate drives a progress bar and is allowed to be missing.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        logger.debug("Ignoring an unreadable Gmail counter %r", value)
        return None
