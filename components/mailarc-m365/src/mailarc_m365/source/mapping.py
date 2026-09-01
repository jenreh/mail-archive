"""Graph's JSON in, the domain's value objects out. Nothing else leaves.

The anti-corruption layer, in one file. ``@odata.nextLink``,
``conversationId``, ``parentFolderId``, ``wellKnownName`` and
``userPrincipalName`` are Microsoft's words for things
:mod:`mailarc_core.mail.model` already has names for, and they stop here — no
dictionary crosses out of this module, which is what keeps a Graph field from
surfacing in the archive two layers away.

Pure functions: no I/O, no status codes, no credentials. Two errors are raised
and both are decisions rather than accidents —
:class:`~mailarc_core.mail.errors.MailPermanentError` for a record Graph sent
malformed, because asking again returns the same malformed record, and
:class:`~mailarc_core.mail.errors.MailCursorExpired` for a stored cursor this
adapter will not follow, because throwing it away and walking the mailbox is
exactly the recovery that error names.
"""

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit

from mailarc_core.mail.errors import MailCursorExpired, MailPermanentError
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
from mailarc_m365.source.model import (
    COUNT,
    DELTA_CHANGE_TYPE,
    DELTA_LINK,
    MESSAGE_SELECT,
    NEXT_LINK,
    REMOVED,
)

logger = logging.getLogger(__name__)


def account_identity(payload: Mapping[str, Any]) -> AccountIdentity:
    """Whose mailbox the token just opened, from a ``GET /me`` reply.

    ``mail`` before ``userPrincipalName``: the two differ more often than not
    — a UPN is a sign-in name and may be an alias, a ``.onmicrosoft.com``
    address, or an on-premises identity — and the one that receives mail is the
    one the archive keys a person on.

    ``id`` is Entra's object id and the only stable handle a mailbox has: an
    address can be renamed, and every message archived under the old one would
    otherwise look like a different account.
    """
    address = _first(payload, "mail", "userPrincipalName")
    return AccountIdentity(
        provider=MailProvider.M365,
        address=EmailAddress(address=address),
        display_name=_optional(payload, "displayName"),
        provider_account_id=_optional(payload, "id") or address,
    )


def configured_identity(
    mailbox: str, display_name: str | None = None
) -> AccountIdentity:
    """Whose mailbox an app-only credential is reading, from the configuration.

    App-only has no ``/me`` — nobody signed in — and ``GET /users/{id}`` needs
    ``User.Read.All``, a *directory* permission an archive has no business
    holding when all it was granted is ``Mail.Read``. So the identity is the
    mailbox the credential names, and :meth:`M365Source.verify` proves that
    name resolves by reading its inbox rather than by reading the directory.

    That is honest about what app-only can know: the address is a configured
    fact rather than a discovered one, and it doubles as
    ``provider_account_id`` because it is the only handle this grant has.
    """
    return AccountIdentity(
        provider=MailProvider.M365,
        address=EmailAddress(address=mailbox),
        display_name=display_name,
        provider_account_id=mailbox,
    )


def labels(payload: Mapping[str, Any]) -> tuple[LabelInfo, ...]:
    """One page of a ``mailFolders`` reply as labels."""
    entries = payload.get("value") or ()
    return tuple(label_info(one) for one in entries)


def label_info(payload: Mapping[str, Any]) -> LabelInfo:
    """One mail folder.

    A Graph folder has no ``type`` the way a Gmail label does, so what makes a
    folder "the provider's own" is ``wellKnownName`` — ``inbox``,
    ``sentitems``, ``deleteditems`` — which Graph fills in for the folders it
    created and leaves out for the ones a person made.

    Deciding on ``displayName`` instead would have been the obvious shortcut
    and is wrong in every mailbox that is not English: Outlook localises those
    names, so a German mailbox's *Posteingang* would be filed as a user folder
    while an English one's *Inbox* would not, and the same archive would
    classify the same folder two ways depending on who looked.

    ``name`` falls back to the id, because a nameless node is worse than one
    named after something a human can at least look up.
    """
    identifier = _required(payload, "id")
    well_known = _optional(payload, "wellKnownName")
    return LabelInfo(
        provider_label_id=identifier,
        name=str(payload.get("displayName") or identifier),
        kind=LabelKind.SYSTEM if well_known else LabelKind.FOLDER,
        message_count=_count(payload.get("totalItemCount")),
    )


def folders_with_children(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The ids in one ``mailFolders`` page that have folders filed under them.

    Graph's own words: *"This API does not return all mail folders in a
    mailbox; to get all folders, each child folder must be traversed
    separately."* ``GET /me/mailFolders`` is the folders directly under the
    root and nothing below them, so an Archive filed inside the Inbox — the
    ordinary shape of a tidy mailbox — is invisible to it, and every message in
    one would reach the graph labelled with a raw folder id.

    ``childFolderCount`` is what makes the traversal cheap: it rides in the
    default projection this adapter already asks for, so the folders worth a
    second request name themselves and a flat mailbox costs no extra call at
    all. A tenant that omits the property simply reports no children, which
    degrades to the flat listing rather than to an error.
    """
    entries = payload.get("value") or ()
    return tuple(
        identifier
        for one in entries
        if isinstance(one, Mapping)
        and _count(one.get("childFolderCount"))
        and (identifier := _optional(one, "id"))
    )


def child_folders_path(mailbox_path: str, folder: str) -> str:
    """Where the folders filed under one folder live.

    Quoted with ``safe=''`` because a folder id is a long base64url string
    Graph mints, and it can carry characters that would otherwise change the
    shape of the path.
    """
    return f"{mailbox_path}/mailFolders/{quote(folder, safe='')}/childFolders"


def message_page(payload: Mapping[str, Any]) -> MessagePage:
    """One ``messages`` listing, with Graph's ``nextLink`` sealed in a cursor.

    The whole URL goes into :class:`~mailarc_core.mail.model.SyncCursor` and
    the engine never looks inside it — that is what the cursor being opaque is
    for, and Graph is the provider that proves it: no other one needs more than
    a token, and teaching the domain model a ``next_url`` field for this one
    would have contradicted the claim that paging is the adapter's business.

    No ``nextLink`` means the walk is over.
    """
    return MessagePage(
        refs=_refs(payload),
        next_cursor=_cursor(payload.get(NEXT_LINK), SyncCursorKind.FULL),
        estimated_total=_count(payload.get(COUNT)),
    )


def delta_page(payload: Mapping[str, Any]) -> MessagePage:
    """One ``messages/delta`` reply as a page of references.

    Graph pages a delta with ``@odata.nextLink`` and finishes it with
    ``@odata.deltaLink``, and only the first of the two is a next cursor. The
    ``deltaLink`` is deliberately *not* returned as one: it would make the
    engine's page loop ask for the same empty delta forever, since following a
    ``deltaLink`` always yields another ``deltaLink``. Where a later run
    resumes from comes from
    :meth:`~mailarc_m365.source.source.M365Source.watermark`, read before the
    walk started and therefore behind everything the walk fetched.

    ``estimated_total`` is the page's own size, because a delta carries no
    total. That is the truth for the one-page delta a scheduled run almost
    always is, and better than leaving it out — the engine reports a running
    maximum against what it has processed, so an understatement on page five of
    a catch-up cannot shrink a total already reported.
    """
    refs = _refs(payload)
    return MessagePage(
        refs=refs,
        next_cursor=_cursor(payload.get(NEXT_LINK), SyncCursorKind.INCREMENTAL),
        estimated_total=len(refs),
    )


def delta_link(payload: Mapping[str, Any]) -> str | None:
    """The ``@odata.deltaLink`` of a finished delta chain, if this is its end."""
    link = payload.get(DELTA_LINK)
    return str(link) if isinstance(link, str) and link else None


def next_link(payload: Mapping[str, Any]) -> str | None:
    """The ``@odata.nextLink`` of a delta chain that has more pages."""
    link = payload.get(NEXT_LINK)
    return str(link) if isinstance(link, str) and link else None


def message_ref(payload: Mapping[str, Any]) -> MessageRef:
    """One message's metadata, from a listing entry or a delta entry.

    The labels are Graph's two different ways of filing a message, and both
    belong on it. ``parentFolderId`` resolves against ``list_labels`` to the
    folder's own name; a *category* has no id at all — in Outlook it **is** its
    name — so it resolves to itself through the engine's fallback, which is
    exactly right: a category is a label a human made.

    ``size_estimate`` stays empty. Graph has a ``size`` property, but including
    it in the projection would cost it on every message of every listing to
    budget a run this adapter fetches one message at a time anyway.
    """
    folder = _optional(payload, "parentFolderId")
    categories = tuple(
        str(one) for one in payload.get("categories") or () if str(one).strip()
    )
    return MessageRef(
        provider_message_id=_required(payload, "id"),
        provider_thread_id=_optional(payload, "conversationId"),
        labels=((folder,) if folder else ()) + categories,
    )


def raw_message(ref: MessageRef, body: bytes) -> RawMessage:
    """The bytes of one message, under the reference the listing gave it.

    The reference comes from the listing rather than from this reply, which is
    the opposite of Gmail's arrangement and forced by the endpoint: ``$value``
    answers with RFC 5322 bytes and no metadata at all. That is also its whole
    virtue — one stdlib parser in :mod:`mailarc_core.mail.parsing` serves every
    provider, and the bytes are what get hashed and stored, so a parser fix can
    be replayed over the archive without asking Microsoft again.
    """
    return RawMessage(ref=ref, raw=body)


def delta_path(mailbox_path: str, folder: str) -> str:
    """Where a fresh delta enumeration starts for one mailbox.

    Graph has **no mailbox-wide message delta**: every documented URL for
    ``messages/delta`` carries a ``mailFolders/{id}`` segment, so the folder is
    part of the address and not a query option. Which folder is
    :attr:`~mailarc_m365.source.config.M365Config.delta_folder`'s business.

    The folder is quoted with ``safe=''`` because it may be a folder *id* — a
    long base64url string Graph mints, which can contain characters that would
    otherwise change the shape of the path.
    """
    return f"{mailbox_path}/mailFolders/{quote(folder, safe='')}/messages/delta"


def delta_params(*, select: str = MESSAGE_SELECT) -> dict[str, str | int]:
    """The query a fresh delta starts with, and every page of it inherits.

    Graph bakes these into the ``nextLink`` and the ``deltaLink`` it hands
    back, so they are chosen once, here, and never repeated on a later page —
    repeating them would produce a URL Graph rejects as having a state token
    and options that disagree.
    """
    return {"changeType": DELTA_CHANGE_TYPE, "$select": select}


def read_cursor_url(cursor: SyncCursor, *, api_base_url: str) -> str:
    """The URL inside a stored cursor, checked before anything is sent to it.

    A cursor is opaque to the engine, which stores it and hands it back
    untouched — so what comes out here is whatever was written into an
    encrypted column, and the next thing that happens to it is a request
    carrying a mailbox's access token. A URL that does not share Graph's own
    origin is therefore refused, and refused as
    :class:`~mailarc_core.mail.errors.MailCursorExpired`, because that is the
    error whose remedy is exactly right for a cursor that cannot be used:
    throw it away and walk the mailbox.

    Also refuses anything that is not an absolute URL. Every Graph cursor is
    one; a bare token in that column is a row written by a different version of
    this adapter, and re-walking is how it recovers.
    """
    token = cursor.token.strip()
    if not token.lower().startswith(("http://", "https://")):
        raise MailCursorExpired(
            "the stored Microsoft 365 cursor is not a Graph link — "
            "walking the mailbox instead"
        )
    if _origin(token) != _origin(api_base_url):
        raise MailCursorExpired(
            f"the stored Microsoft 365 cursor points at {_origin(token)}, "
            f"not at {_origin(api_base_url)} — walking the mailbox instead"
        )
    return token


def cursor_for(url: str, kind: SyncCursorKind) -> SyncCursor:
    """A whole Graph link as the cursor the engine will hand back."""
    return SyncCursor(provider=MailProvider.M365, token=url, kind=kind)


def _cursor(link: object, kind: SyncCursorKind) -> SyncCursor | None:
    """A ``nextLink`` as a cursor, or ``None`` when the page was the last one."""
    if not isinstance(link, str) or not link:
        return None
    return cursor_for(link, kind)


def _refs(payload: Mapping[str, Any]) -> tuple[MessageRef, ...]:
    """Every message in a ``value`` array, minus the ones Graph reports as gone.

    ``@removed`` cannot appear while the delta asks for
    :data:`~mailarc_m365.source.model.DELTA_CHANGE_TYPE` alone, and is skipped
    regardless: an archive that deleted on Microsoft's say-so would not be an
    archive. A deleted entry also carries no properties beyond an id, so
    mapping one would raise on the fields that are not there.
    """
    entries = payload.get("value")
    if not isinstance(entries, list):
        raise MailPermanentError(
            f"Graph's reply carries {type(entries).__name__} where a list of "
            "messages belongs"
        )
    return tuple(
        message_ref(_object(one, "message"))
        for one in entries
        if REMOVED not in _object(one, "message")
    )


def _object(value: object, what: str) -> Mapping[str, Any]:
    """A nested JSON object, or a skipped page.

    Microsoft documents these shapes; a reply that does not have them is one
    this adapter cannot read, and asking again returns the same reply. So it is
    a :class:`~mailarc_core.mail.errors.MailPermanentError` like every other
    malformed record here — never a silent skip, which would drop new mail and
    leave nothing behind to notice it by.
    """
    if not isinstance(value, Mapping):
        raise MailPermanentError(
            f"Graph's {what} is {type(value).__name__} and not an object"
        )
    return value


def _required(payload: Mapping[str, Any], key: str) -> str:
    """A string field the rest of the mapping cannot do without."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MailPermanentError(f"Graph's reply carries no {key}")
    return value


def _first(payload: Mapping[str, Any], *keys: str) -> str:
    """The first of these fields that is a non-empty string."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise MailPermanentError(f"Graph's reply carries none of {', '.join(keys)}")


def _optional(payload: Mapping[str, Any], key: str) -> str | None:
    """A string field that is allowed to be missing."""
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _count(value: object) -> int | None:
    """A Graph counter as an ``int``, or ``None`` when it is neither.

    ``@odata.count`` only appears when a caller asked for ``$count``, and the
    full walk in :meth:`~mailarc_m365.source.source.M365Source.list_messages`
    does — it is the mailbox size the progress bar divides by. A delta cannot
    ask (``messages/delta`` takes no ``$count``), which is why
    :func:`delta_page` counts its own references instead.

    ``None`` for anything that is not a number, including a page that carries
    no count at all: the engine reads that as "keep the estimate you have",
    which is the right answer for a ``nextLink`` Graph chose not to repeat the
    count on.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        logger.debug("Ignoring an unreadable Graph counter %r", value)
        return None


def _origin(url: str) -> str:
    """Scheme and host of a URL, lowercased, port included. Never the path."""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{(parts.netloc or '').lower()}"
