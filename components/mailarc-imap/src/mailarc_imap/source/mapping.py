"""IMAP's numbers in, the domain's value objects out. Nothing else leaves.

The anti-corruption layer of §3.1, in one file. ``UIDVALIDITY``, ``UIDNEXT``,
the ``\\Noselect`` flag and the bare integers a UID search answers with are
IMAP's words for things :mod:`mailarc_core.mail.model` already has names for,
and they stop here.

Two conventions live in this module and nowhere else, both for the same reason
``mailarc_google.source.mapping`` keeps its history cursor in one place: a
format spread across call sites drifts, and both of these are formats whose
drift is silent.

**The cursor.** ``UIDVALIDITY`` and the next UID to look at, joined by
:data:`CURSOR_SEPARATOR`, minted by :func:`cursor` and read by
:func:`read_cursor`.

**The message id.** The folder, the ``UIDVALIDITY`` and the UID, joined by
:data:`ID_SEPARATOR`, minted by :func:`message_id` and read by
:func:`read_message_id`.

Pure functions: no sockets, no credentials, no ``imapclient``. The only errors
raised are :class:`~mailarc_core.mail.errors.MailPermanentError` for a
reference this adapter did not write and cannot read, and never a silent skip.
"""

import logging

from pydantic import BaseModel, ConfigDict

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
from mailarc_imap.source.credentials import ImapCredentials
from mailarc_imap.source.model import FetchedBody, FolderListing, FolderState

logger = logging.getLogger(__name__)

CURSOR_SEPARATOR = ":"
"""Joins ``UIDVALIDITY`` to the next UID, and appears in neither half.

Both halves are decimal, so a colon belongs to neither alphabet and cannot show
up inside one. It is the same character IMAP itself uses for a UID range, which
is a coincidence worth naming: the token reads like ``1738:4201`` and means
"generation 1738, resume at UID 4201", not a range.
"""

ID_SEPARATOR = ":"
"""Joins the three parts of a ``provider_message_id``.

Read from the **right**, because a folder name may legally contain a colon —
``[Gmail]/All Mail`` does not, but a hand-made folder called ``Notes: 2024``
does, and a parser that split from the left would hand the fetch a UID of
``2024``.
"""

FIRST_UID = 1
"""Where a walk with no cursor starts. IMAP UIDs are 1-based and never reused."""


class CursorPosition(BaseModel):
    """A cursor's two numbers, once they have been read back out of its token."""

    model_config = ConfigDict(frozen=True)

    uidvalidity: int
    next_uid: int


class MessageAddress(BaseModel):
    """A ``provider_message_id``, once it has been read back apart.

    ``uidvalidity`` is carried in the id rather than only in the cursor, and
    that is the point of the id having three parts. The archive's ledger keys
    on ``(account, provider_message_id)``: if the id were the bare UID, a
    server that renumbered the folder would issue UID 5 to a *different*
    message, the ledger would recognise it as one it already has, and that
    message would never be fetched. Silent, permanent, and reported as
    "skipped, already archived". The folder is in there for the same reason one
    step out — two folders on one server routinely share a ``UIDVALIDITY``, so
    a person moving an account from ``INBOX`` to ``[Gmail]/All Mail`` would
    otherwise have most of All Mail recognised as already archived.
    """

    model_config = ConfigDict(frozen=True)

    folder: str
    uidvalidity: int
    uid: int


def identity(credentials: ImapCredentials) -> AccountIdentity:
    """Whose mailbox the login just opened.

    IMAP has no ``getProfile``: there is no command that asks the server who
    the client is, so the authenticated username is the only answer available
    and it is an honest one — it is what the server checked the password
    against. That makes this the one ``verify`` in the project whose result the
    provider did not compute, which is why
    :meth:`~mailarc_imap.source.source.ImapSource.verify` still talks to the
    server: the value comes from here, the *proof* comes from the login.

    ``provider_account_id`` is the username and the host together. A username
    alone is not unique — ``jens`` on two different mail hosts is two mailboxes
    — and this field is what a later reader would use to tell two accounts
    apart.

    ``display_name`` stays ``None``, and that is the field worth being careful
    with. The domain calls it the name of *whoever the credentials belong to*,
    and IMAP has no command that reports one: ``LOGIN`` answers ``OK`` and
    nothing else. Anything put here would therefore be a different fact wearing
    this field's name — the folder, say, which would render as a person called
    ``INBOX`` the first time somebody showed the identity to a human. The
    absence is the honest answer, and the domain makes the field optional for
    exactly the providers that have to give it.
    """
    address = EmailAddress(address=credentials.username)
    return AccountIdentity(
        provider=MailProvider.IMAP,
        address=address,
        provider_account_id=f"{credentials.username}@{credentials.host}",
    )


def labels(listings: tuple[FolderListing, ...]) -> tuple[LabelInfo, ...]:
    """Every selectable folder, as a label of kind ``FOLDER``.

    ``LabelKind.FOLDER`` exists in the domain for exactly this — an IMAP
    mailbox pretending to be a label — so nothing here has to decide between
    ``SYSTEM`` and ``USER``. IMAP does not draw that line: ``INBOX`` is the one
    name RFC 3501 reserves and everything else is whatever the server or the
    user made, with no field that says which.

    ``\\Noselect`` names are dropped. They are containers — ``[Gmail]`` itself,
    the intermediate nodes of a hierarchy — and offering one would be offering
    a folder that answers ``SELECT`` with ``NO``.

    ``message_count`` is left unset for all of them. Filling it in means a
    ``STATUS`` command **per folder**, which on a Gmail account with thirty
    labels is thirty extra round trips on every run, for a number the engine
    uses only to decorate a list. The field is optional in the domain for
    providers that have to pay for it.
    """
    return tuple(folder_label(listing) for listing in listings if listing.selectable())


def folder_label(listing: FolderListing) -> LabelInfo:
    """One folder as a label. The name is the id: IMAP issues no folder ids."""
    return LabelInfo(
        provider_label_id=listing.name,
        name=listing.name,
        kind=LabelKind.FOLDER,
    )


def cursor(uidvalidity: int, next_uid: int, kind: SyncCursorKind) -> SyncCursor:
    """The two numbers a resume needs, sealed into one opaque token.

    ``next_uid`` always means the same thing in both kinds — *the first UID
    this run has not looked at yet* — which is what lets one format serve a
    full walk and a delta. The kind is still carried, because it is the only
    thing that can tell
    :meth:`~mailarc_imap.source.source.ImapSource.list_messages` what a
    mismatched ``uidvalidity`` should do about itself.
    """
    return SyncCursor(
        provider=MailProvider.IMAP,
        token=f"{uidvalidity}{CURSOR_SEPARATOR}{next_uid}",
        kind=kind,
    )


def read_cursor(stored: SyncCursor) -> CursorPosition | None:
    """The two numbers back out of one token, or ``None`` for a token that is not one.

    ``None`` rather than an exception, because the caller has to make the same
    decision for an unreadable cursor as for one from a different
    ``UIDVALIDITY`` — a delta cannot resume and says
    :class:`~mailarc_core.mail.errors.MailCursorExpired`, a full walk starts
    over — and returning a value keeps that decision in the one place that
    knows which of the two it is doing. Raising here would put half of it in
    this module and half in that one.

    A token that is not this provider's is the case worth naming: cursors are
    stored per account in a column no migration has ever touched, so a mailbox
    that was a Gmail account before somebody re-added it as IMAP has a
    ``historyId`` sitting where this expects a pair.
    """
    generation, separator, following = stored.token.partition(CURSOR_SEPARATOR)
    if not separator:
        logger.debug("An IMAP cursor without a separator: %r", stored.token)
        return None
    try:
        return CursorPosition(
            uidvalidity=int(generation), next_uid=max(int(following), FIRST_UID)
        )
    except ValueError:
        logger.debug("An IMAP cursor that is not two numbers: %r", stored.token)
        return None


def message_id(folder: str, uidvalidity: int, uid: int) -> str:
    """The archive's handle for one message. See :class:`MessageAddress` for why."""
    return f"{folder}{ID_SEPARATOR}{uidvalidity}{ID_SEPARATOR}{uid}"


def read_message_id(ref: MessageRef) -> MessageAddress:
    """A reference back apart, so the fetch knows which UID to ask for.

    A reference this adapter did not mint is a message it cannot fetch, and
    asking the server again would not change that — so it is a
    :class:`~mailarc_core.mail.errors.MailPermanentError` like every other
    unreadable record, never a silent skip (§7.6). It reaches here only if
    something handed the engine a page from one provider and a fetch from
    another.
    """
    rest, separator, tail = ref.provider_message_id.rpartition(ID_SEPARATOR)
    folder, generation_separator, generation = rest.rpartition(ID_SEPARATOR)
    if not separator or not generation_separator:
        raise MailPermanentError(
            f"{ref.provider_message_id!r} is not an IMAP message reference"
        )
    try:
        return MessageAddress(folder=folder, uidvalidity=int(generation), uid=int(tail))
    except ValueError as error:
        raise MailPermanentError(
            f"{ref.provider_message_id!r} is not an IMAP message reference"
        ) from error


def message_ref(state: FolderState, uid: int) -> MessageRef:
    """One UID as a reference the engine can carry around.

    ``provider_thread_id`` stays ``None``, and that is a decision rather than
    an omission. IMAP has no thread ids — ``THREAD`` is an optional extension
    that computes them from the headers this archive is about to parse anyway —
    so :mod:`mailarc_core.mail.parsing` derives ``thread_hint`` from
    ``References`` and ``In-Reply-To``, which the domain model documents as
    "what IMAP has instead". Inventing one here would put a guess where the
    domain expects a fact and would beat the real answer, because the
    provider's own thread id always wins where there is one.

    ``labels`` carries the folder, because for this adapter the folder *is* the
    only label a message has: one account syncs one folder
    (:data:`~mailarc_imap.source.model.IMAP_DESCRIPTOR`), and IMAP's per-message
    flags are ``\\Seen`` and ``\\Flagged``, which say what the owner did with
    the mail rather than where it belongs.

    ``size_estimate`` is left unset. It would cost a ``FETCH RFC822.SIZE`` for
    every UID in the page — a second round trip over the whole listing, for a
    number that only budgets a progress bar — and it arrives for free beside
    the bytes in :func:`raw_message`.
    """
    return MessageRef(
        provider_message_id=message_id(state.folder, state.uidvalidity, uid),
        labels=(state.folder,),
    )


def message_page(
    state: FolderState,
    uids: tuple[int, ...],
    *,
    limit: int,
    kind: SyncCursorKind,
) -> MessagePage:
    """One slice of a UID search, with the resume point sealed in a cursor.

    ``UID SEARCH`` has no server-side paging: it answers with every matching
    UID at once, which is one line and cheap even for a large mailbox, and the
    paging the port asks for is this slice. The next cursor names the UID after
    the last one handed over — never the last one itself, which would deliver
    it twice — and it is ``None`` when the slice reached the end, because a
    cursor that is never ``None`` leaves the engine's page loop spinning.

    ``estimated_total`` is what is left from here, which for the first page of
    a walk is the whole mailbox and for later pages is less. The engine reports
    a running maximum against what it has processed
    (``ImportEngine._estimate``), so a shrinking number does not make the
    progress row go backwards — and this is the only estimate there is.
    """
    page = uids[:limit]
    following = uids[len(page) :]
    return MessagePage(
        refs=tuple(message_ref(state, uid) for uid in page),
        next_cursor=(
            cursor(state.uidvalidity, page[-1] + 1, kind)
            if following and page
            else None
        ),
        estimated_total=len(uids),
    )


def raw_message(ref: MessageRef, body: FetchedBody) -> RawMessage:
    """The bytes, under the reference the listing already handed the engine.

    The reference is carried over rather than rebuilt, and for once that is the
    right way round: ``ImportEngine._fetch_slice`` tracks what a stream
    delivered by ``raw.ref.provider_message_id`` and retries whatever is
    missing, so a fetch that answered under a *different* id would be retried
    for ever. Gmail rebuilds its reference here because its ``format=raw``
    reply is richer than its listing entry; an IMAP fetch carries nothing the
    listing did not already know except the size.
    """
    return RawMessage(
        ref=ref.model_copy(update={"size_estimate": body.size}), raw=body.raw
    )
