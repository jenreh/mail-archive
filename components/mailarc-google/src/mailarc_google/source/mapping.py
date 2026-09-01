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
from collections.abc import Iterator, Mapping
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

HISTORY_CURSOR_SEPARATOR = "|"
"""Joins the two halves of a paging history cursor, and appears in neither.

``users.history.list`` wants ``startHistoryId`` on **every** request and
``pageToken`` on every one after the first, while
:class:`~mailarc_core.mail.model.SyncCursor` has a single ``token``. Sealing
both into that one string is exactly what the cursor's "opaque to the engine"
licenses, and it is cheaper than teaching the domain model a ``page_token``
field that only one provider would ever fill — a field which would also
contradict :class:`~mailarc_core.mail.model.MessagePage`, whose whole claim is
that paging is the adapter's business.

A history id is decimal and a page token is base64url, so a bar belongs to
neither alphabet and cannot appear inside a half.
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


def account_watermark(payload: Mapping[str, Any]) -> SyncCursor:
    """Where a delta would start right now, from a ``users.getProfile`` reply.

    A sibling of :func:`account_identity` rather than a field on it. The two
    read the same reply and mean different things: an identity says *whose*
    mailbox this is and lives on the account row forever, a watermark says *how
    far* it has been read and is stale the moment the next mail arrives. Giving
    :class:`~mailarc_core.mail.model.AccountIdentity` a ``history_id`` would
    also teach the domain a Gmail word, which is the one thing this module
    exists to prevent.

    A profile without a ``historyId`` is a malformed profile and is treated
    exactly like one without an address. Returning ``None`` instead would be
    the more forgiving answer and the wrong one: it says "this mailbox has no
    delta", contradicting
    :data:`~mailarc_google.source.model.GMAIL_DESCRIPTOR`, and the scheduler
    would go on queueing runs that fetch nothing.
    """
    return history_cursor(_required(payload, "historyId"))


def history_cursor(start_history_id: str, page_token: str | None = None) -> SyncCursor:
    """The two halves of a history walk, sealed into one opaque token.

    Minted here and read back by :func:`read_history_cursor`, and nowhere else
    — the convention has one home so the separator cannot drift away from the
    split. The engine only ever stores the half without a page token: it
    checkpoints the incremental scope once, at the end of a run, from
    ``watermark()`` rather than from a page. A page token in that column would
    come back as a ``startHistoryId`` and 404, which turns every scheduled
    delta into a full re-walk while looking perfectly healthy.
    """
    token = start_history_id
    if page_token:
        token = f"{start_history_id}{HISTORY_CURSOR_SEPARATOR}{page_token}"
    return SyncCursor(
        provider=MailProvider.GMAIL,
        token=token,
        kind=SyncCursorKind.INCREMENTAL,
    )


def read_history_cursor(cursor: SyncCursor) -> tuple[str, str | None]:
    """The ``startHistoryId`` and the ``pageToken`` back out of one token.

    A cursor with no separator is a watermark — the first call of a delta,
    which has a start but no page yet.
    """
    start, separator, page_token = cursor.token.partition(HISTORY_CURSOR_SEPARATOR)
    return start, page_token if separator else None


def history_page(payload: Mapping[str, Any], *, start_history_id: str) -> MessagePage:
    """One ``users.history.list`` reply, as a page of references.

    Only ``messagesAdded`` is read, because that is all the adapter asks for
    and all an archive can act on: it never deletes and it re-reads labels from
    the message itself. The reply's own ``historyId`` is deliberately ignored —
    it goes nowhere near ``next_cursor``. A cursor that is never ``None`` would
    leave the engine's page loop spinning against a live quota, and the point a
    later delta resumes from comes from ``watermark()``, read before the walk
    started and therefore behind everything the walk fetched.

    Gmail lists the same message under several history records — added, then
    labelled, then added to a thread — so the ids are deduplicated. The first
    sighting wins; a later one carries no better reference, since the labels
    that reach the graph come from the ``format=raw`` fetch (:func:`raw_message`).

    ``estimated_total`` is the page's own size, because a history reply carries
    no ``resultSizeEstimate``. That is the truth for the one-page delta a
    scheduled run almost always is, and better than leaving it out, which would
    have the progress row keep the total of the last full import and report one
    new mail as ``1 / 12000``. For a multi-page catch-up it is an
    understatement, and page five's hundred would overwrite a total of eight
    hundred already done — so the engine reports the running maximum against
    what it has processed (``ImportEngine._estimate``) rather than this number
    raw. Both halves are needed: this one is the only estimate there is, and
    that one is the only place that knows what came before.
    """
    refs: dict[str, MessageRef] = {}
    for message in _added_messages(payload):
        ref = message_ref(message)
        refs.setdefault(ref.provider_message_id, ref)
    token = payload.get("nextPageToken")
    return MessagePage(
        refs=tuple(refs.values()),
        next_cursor=(history_cursor(start_history_id, str(token)) if token else None),
        estimated_total=len(refs),
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


def _added_messages(payload: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Every ``messagesAdded[].message`` of a history reply, records flattened.

    Two levels of nesting that carry no information the caller needs: a page is
    a list of history records, each record a list of changes, and only the
    message inside a change matters here. A reply with no ``history`` at all is
    the normal answer to "anything new?" and yields nothing.
    """
    for record in payload.get("history") or ():
        changes = _object(record, "history record").get("messagesAdded") or ()
        for change in changes:
            added = _object(change, "messagesAdded entry")
            yield _object(added.get("message"), "messagesAdded entry's message")


def _object(value: object, what: str) -> Mapping[str, Any]:
    """A nested JSON object, or a skipped page.

    Google documents these shapes; a reply that does not have them is one this
    adapter cannot read, and asking again returns the same reply. So it is a
    :class:`~mailarc_core.mail.errors.MailPermanentError` like every other
    malformed record here — never a silent skip, which would drop new mail and
    leave nothing behind to notice it by (§7.6).
    """
    if not isinstance(value, Mapping):
        raise MailPermanentError(
            f"Gmail's {what} is {type(value).__name__} and not an object"
        )
    return value


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
