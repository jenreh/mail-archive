"""Microsoft 365 as :class:`~mailarc_core.mail.ports.MailSourcePort`.

The sibling of :class:`~mailarc_google.source.source.GmailSource` and of
``FakeMailSource``, and shaped like them on purpose: a class attribute for the
provider, a ``create`` that is the
:data:`~mailarc_core.mail.ports.MailSourceFactory`, a descriptor the
composition root registers, and ``fetch_raw`` as a coroutine that *returns* a
stream. Implementations that look alike are what make the port a port.

Messages always come back as raw MIME, from ``/messages/{id}/$value``. One
stdlib parser in :mod:`mailarc_core.mail.parsing` then serves every provider,
and the bytes are what get hashed and stored — so a parser fix can be replayed
over the archive without asking Microsoft again. Graph's own JSON
representation of a message is never mapped onto ``ParsedMessage``.

**The two modes differ in the path and nowhere else.** A delegated credential
reads ``/me``; an app-only one reads ``/users/{address}``, because a token
nobody signed in to has no ``/me`` to resolve. That prefix comes off the
credential (:attr:`~mailarc_m365.source.credentials.M365DelegatedCredentials.mailbox_path`),
so every method below is written once. The single place the modes genuinely
diverge is :meth:`M365Source.verify`, and it diverges because the permission
sets do: ``Mail.Read`` as an *application* permission grants a mailbox and not
the directory, so there is no user object to read.

This class owns no state beyond its client. Which account, whose credentials
and how far the last run got are rows in SQLite; the delta's position travels
inside the cursor the engine hands back, never in an attribute here, because an
attribute would make a run resumed by a second worker list the wrong window.
"""

import logging
from collections import deque
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import quote

from mailarc_core.mail.errors import (
    MailAuthError,
    MailCursorExpired,
    MailPermanentError,
)
from mailarc_core.mail.model import (
    AccountIdentity,
    LabelInfo,
    MailProvider,
    MessagePage,
    MessageRef,
    RawMessage,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_core.mail.ports import MailSourceFactory, MailSourcePort
from mailarc_m365.source import mapping
from mailarc_m365.source.client import GraphClient
from mailarc_m365.source.config import M365Config
from mailarc_m365.source.credentials import (
    M365AppOnlyCredentials,
    M365Credentials,
    from_secret,
)
from mailarc_m365.source.model import M365_DESCRIPTOR, MESSAGE_SELECT

logger = logging.getLogger(__name__)

GRAPH_MAX_PAGE_SIZE = 1000
"""Graph's own ceiling for ``$top`` on messages; asking for more is a 400."""

MAX_FOLDER_PAGES = 50
"""How many requests one ``list_labels`` will spend on folders.

A budget over the whole traversal — pages of one collection and descents into
child folders alike — because ``mailFolders`` defaults to ten per page *and*
returns only the root level, so both loops draw on the same purse. A mailbox
with five hundred folders is a filing habit; five thousand is a bug somewhere,
and an unbounded walk over a paginated, self-referencing endpoint is how one
worker spends an afternoon.
"""

IDENTITY_SELECT = "id,mail,userPrincipalName,displayName"
"""The four properties :meth:`M365Source.verify` needs off a user.

``/me`` without a ``$select`` returns two dozen directory properties — job
title, office location, business phones — that this application has no use for
and no business logging.
"""

INBOX = "inbox"
"""The well-known folder an app-only ``verify()`` reads to prove its grant."""


class M365Source:
    """One Microsoft 365 mailbox, behind the six methods the engine knows."""

    provider = MailProvider.M365
    DESCRIPTOR = M365_DESCRIPTOR

    def __init__(
        self, credentials: M365Credentials, config: M365Config | None = None
    ) -> None:
        self._config = config or M365Config()
        self._client = GraphClient(credentials, self._config)

    @classmethod
    def create(cls, account: Any, secret: str) -> MailSourcePort:
        """The :data:`~mailarc_core.mail.ports.MailSourceFactory` for this provider.

        ``secret`` is the decrypted ``mail_credentials.secret``, which here is
        the JSON of one of the two credential shapes. The account row is
        unused: a delegated mailbox says whose it is itself in :meth:`verify`,
        and an app-only one carries the address it was given.

        Configuration comes from the environment. A composition root that
        builds its own :class:`~mailarc_m365.source.config.M365Config`
        registers :meth:`using` instead.
        """
        return cls.using(M365Config())(account, secret)

    @classmethod
    def using(cls, config: M365Config) -> MailSourceFactory:
        """A factory bound to one configuration, for the composition root.

        ``app/composition.py`` is the only module allowed to build a component
        from configuration, and the factory signature has no room for one — so
        the config is closed over here rather than looked up later.
        """

        def build(account: Any, secret: str) -> MailSourcePort:
            return cls(from_secret(secret), config)

        return build

    @property
    def credentials(self) -> M365Credentials:
        """The credentials as they stand, refreshes and rotations included.

        ``app/worker.py`` reads ``to_secret()`` off this at the end of a run
        and writes it back when it changed, so a refresh token Entra rotated
        mid-import is not lost. This is the only copy of it.
        """
        return self._client.credentials

    async def verify(self) -> AccountIdentity:
        """Prove the credentials work and report whose mailbox this is.

        The one method where the two modes take different calls, because the
        permissions they hold are different things. A delegated token carries
        ``User.Read`` and can simply ask ``/me`` who it is. An app-only token
        carries ``Mail.Read`` over *mailboxes* and nothing over the directory,
        so ``GET /users/{id}`` would be a 403 — asking for ``User.Read.All`` to
        avoid it would mean an archive holding a permission to read every
        person in the tenant, which is a far larger grant than the one thing it
        needs. Instead it reads the mailbox's own inbox, which is precisely the
        permission it was given, and reports the address it was configured
        with.

        A 404 on that probe is named as an auth failure rather than left at
        the default. Nothing about it is "skip this one and carry on": the
        address in the Mailbox field is not a mailbox this grant can open, and
        the account has to go to ``auth_error`` so the page that holds the
        field offers itself again. ``MailPermanentError`` is the instruction
        to drop one *message*, and there is no message here.
        """
        credentials = self._client.credentials
        if isinstance(credentials, M365AppOnlyCredentials):
            await self._client.get_json(
                f"{credentials.mailbox_path}/mailFolders/{INBOX}",
                not_found=MailAuthError,
            )
            identity = mapping.configured_identity(credentials.mailbox)
        else:
            identity = mapping.account_identity(
                await self._client.get_json(
                    credentials.mailbox_path, params={"$select": IDENTITY_SELECT}
                )
            )
        logger.debug(
            "Microsoft 365 credentials (%s) belong to %s",
            credentials.mode.value,
            identity.address.address,
        )
        return identity

    async def list_labels(self) -> Sequence[LabelInfo]:
        """Every mail folder of the account — nested ones included.

        Two loops in one, and both of them are forced by Graph.

        It **pages**, because ``mailFolders`` hands back ten at a time by
        default where Gmail's ``labels.list`` returns everything: a mailbox
        with twelve folders would otherwise archive every message in the last
        two under a bare folder id.

        It also **descends**, which is the half that is easy to miss.
        ``GET /me/mailFolders`` returns the folders directly under the root and
        stops there — Microsoft says so in as many words — so an Archive filed
        inside the Inbox is not in that reply, and neither is anything a person
        organised into subfolders. Since the port's contract is *every* label
        the account has, each folder that reports a ``childFolderCount`` is
        followed into its ``childFolders``. A flat mailbox costs exactly the
        same one request it did before.

        Deliberately without a ``$select``. ``wellKnownName`` is what tells a
        provider folder from a person's (see
        :func:`~mailarc_m365.source.mapping.label_info`) and ``childFolderCount``
        is what makes the descent cheap; both are part of the default
        projection, and naming them explicitly would buy a smaller body and
        risk a 400 on a tenant whose Graph does not offer one of them, which
        would take every label with it.

        :data:`MAX_FOLDER_PAGES` bounds the requests rather than the depth, so
        a filing habit that is deep and a filing habit that is wide cost the
        same ceiling — and a mailbox whose folders somehow refer to each other
        cannot spend an afternoon here.
        """
        found: list[LabelInfo] = []
        seen: set[str] = set()
        pending: deque[tuple[str, dict[str, str | int] | None]] = deque(
            [(f"{self._mailbox}/mailFolders", {"$top": self._folder_page_size()})]
        )
        for _ in range(MAX_FOLDER_PAGES):
            if not pending:
                return tuple(found)
            target, params = pending.popleft()
            payload = await self._client.get_json(target, params=params)
            for label in mapping.labels(payload):
                if label.provider_label_id in seen:
                    continue
                seen.add(label.provider_label_id)
                found.append(label)
            following = mapping.next_link(payload)
            if following is not None:
                # Ahead of the children so one collection is read whole before
                # the descent starts; the link carries the query it was made
                # with, so it must not be given one again.
                pending.appendleft((following, None))
            pending.extend(
                (
                    mapping.child_folders_path(self._mailbox, parent),
                    {"$top": self._folder_page_size()},
                )
                for parent in mapping.folders_with_children(payload)
            )
        logger.warning(
            "Stopped reading Microsoft 365 folders after %d requests; a message "
            "in a folder below that is labelled by its id",
            MAX_FOLDER_PAGES,
        )
        return tuple(found)

    async def list_messages(
        self, cursor: SyncCursor | None, *, limit: int
    ) -> MessagePage:
        """One page of references; ``None`` starts at the top of the mailbox.

        The cursor's ``kind`` picks the endpoint, and it is the only thing that
        can: the engine hands back whatever it was given, so a cursor minted by
        :meth:`watermark` or by a previous delta page keeps the run on
        ``messages/delta``, while a ``nextLink`` from a full walk keeps it on
        ``messages``. Reading the *engine's* mode instead would need a seventh
        port argument for something the cursor already says.

        A full-walk cursor this adapter will not follow starts the walk over
        instead of raising; see :meth:`_resumable` for why that is the only
        recovery the engine leaves open.

        Everything the mailbox holds, Deleted Items included — which is where
        this differs from Gmail, whose ``messages.list`` hides spam and trash
        by default. Keeping them is the archive's whole premise: a mail deleted
        in Outlook next week was still received this week, and an archive that
        forgot it the moment somebody tidied up would be a mirror, not a
        record.
        """
        if cursor is not None and cursor.kind is SyncCursorKind.INCREMENTAL:
            return await self._list_delta(cursor)
        if cursor is not None:
            following = self._resumable(cursor)
            if following is not None:
                return mapping.message_page(await self._client.get_json(following))
        return mapping.message_page(
            await self._client.get_json(
                f"{self._mailbox}/messages",
                params={
                    "$top": self._page_size(limit),
                    "$select": MESSAGE_SELECT,
                    # Newest first, said out loud rather than inherited from
                    # Graph's default. The engine's resume logic rests on this
                    # ordering — a resumed walk carries on into older mail and
                    # inherits the mark of the attempt that began it — and a
                    # default is not a promise.
                    "$orderby": "receivedDateTime desc",
                },
            )
        )

    async def watermark(self) -> SyncCursor | None:
        """Where a delta would start if it started now — a fresh ``deltaLink``.

        Never ``None``, which is what
        :data:`~mailarc_m365.source.model.M365_DESCRIPTOR` promises with
        ``supports_incremental``.

        **Graph makes this expensive and there is no cheaper honest answer.** A
        ``deltaLink`` is only ever handed out at the *end* of a delta chain, so
        obtaining one means walking the chain — Microsoft's own initial-sync
        guidance says exactly that. There is no ``$deltatoken=latest`` for
        Outlook resources; that shortcut exists for SharePoint and OneDrive
        only, and using it here would be inventing a supported parameter.

        So this drains the chain, and the drain is made as cheap as Graph
        allows: only ``created`` changes, a page size well above the listing's,
        and the same projection the delta itself will use — the same one,
        because the query is baked into the link Graph returns, and a drain
        that selected less would mint a cursor whose later pages carry no
        folder and no categories.

        Reaching :attr:`~mailarc_m365.source.config.M365Config.watermark_max_pages`
        is not a failure. The ``nextLink`` it stopped at is itself a legal
        incremental cursor, so the mark simply lands mid-chain and the next run
        carries the enumeration a little further. Every position in a delta
        chain is behind everything that has not been enumerated yet, which is
        the only property a watermark has to have.
        """
        target = mapping.delta_path(self._mailbox, self._config.delta_folder)
        params: dict[str, str | int] | None = mapping.delta_params()
        headers = {"Prefer": f"odata.maxpagesize={self._config.watermark_page_size}"}
        for _ in range(max(1, self._config.watermark_max_pages)):
            payload = await self._client.get_json(
                target, params=params, headers=headers
            )
            params = None  # every link Graph returns carries the query already
            finished = mapping.delta_link(payload)
            if finished is not None:
                return mapping.cursor_for(finished, SyncCursorKind.INCREMENTAL)
            following = mapping.next_link(payload)
            if following is None:
                raise MailPermanentError(
                    f"Graph's delta at {target} ended without a deltaLink and "
                    "without a nextLink"
                )
            target = following
        logger.info(
            "Microsoft 365 delta for this mailbox is still enumerating after "
            "%d pages; the watermark lands mid-chain and the next run resumes it",
            self._config.watermark_max_pages,
        )
        return mapping.cursor_for(target, SyncCursorKind.INCREMENTAL)

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        """The RFC 5322 bytes for those references, as they arrive.

        A coroutine returning a stream, not an async generator: the engine
        calls it as ``async for raw in await source.fetch_raw(refs)``, and an
        async generator would make that ``await`` fail at the call site.
        """
        return self._stream(refs)

    async def aclose(self) -> None:
        """Release the HTTP client. Safe to call twice."""
        await self._client.aclose()

    @property
    def _mailbox(self) -> str:
        """``/me`` or ``/users/{address}`` — the prefix every path hangs off.

        Read off the credential on every use rather than cached, because a
        refresh replaces the credential object; the prefix itself never
        changes, but a stale reference to the old object would.
        """
        return self._client.credentials.mailbox_path

    async def _list_delta(self, cursor: SyncCursor) -> MessagePage:
        """What arrived since a stored ``deltaLink``, one page at a time.

        The ``gone`` is this method's whole point. Graph answers a token it no
        longer keeps with **410 and ``resyncRequired``**, and the remedy it
        names is a full resynchronisation. Left at the default, that would
        reach the engine as a
        :class:`~mailarc_core.mail.errors.MailPermanentError`, be filed as one
        skipped message that never existed, and the account would quietly stop
        syncing while every run reported success. Only the call site knows it
        asked a delta question, which is why the meaning travels in rather than
        the status code travelling out.

        A 404 stays what it is. A delta URL that 404s is a folder that has been
        deleted or renamed away, not a token that aged out, and re-walking the
        whole mailbox would not bring the folder back.
        """
        return mapping.delta_page(
            await self._client.get_json(
                self._cursor_url(cursor), gone=MailCursorExpired
            )
        )

    def _cursor_url(self, cursor: SyncCursor) -> str:
        """The link inside a cursor, refused if it does not point at Graph."""
        return mapping.read_cursor_url(cursor, api_base_url=self._config.api_base_url)

    def _resumable(self, cursor: SyncCursor) -> str | None:
        """A full walk's stored link, or ``None`` to start the walk again.

        The one place :class:`~mailarc_core.mail.errors.MailCursorExpired` is
        caught rather than raised, and the reason is the engine's own handler:
        it recovers from an expired cursor by falling back to a full walk, and
        for a run that *is* a full walk it re-raises instead — "a provider
        refusing that is refusing the walk itself". That reading is right for a
        provider that refused to start from the top, and wrong for the only
        thing that raises it here, which is the *stored page token* being
        unusable: not a Graph URL at all, or pointing at an origin no bearer
        token may go to. The engine never clears the full checkpoint on that
        path, so the same row would be read, refused and re-raised on every run
        for ever — an account that fails silently and permanently on a value
        that is simply stale.

        So the walk restarts from the top, which is precisely what
        ``MailCursorExpired`` asks for, and the first page overwrites the bad
        row with a link Graph just minted. Re-listing costs listing calls and
        no mail: the engine's ledger filter and the writer's get-before-add
        make every already-archived message a no-op.

        A delta cursor is *not* routed through here. There the engine's
        fallback is exactly the right one, and letting the error out is how it
        is reached.
        """
        try:
            return self._cursor_url(cursor)
        except MailCursorExpired as expired:
            logger.warning(
                "Microsoft 365 full-walk checkpoint is unusable (%s); "
                "listing this mailbox from the top instead",
                expired,
            )
            return None

    def _page_size(self, limit: int) -> int:
        """The smallest of the engine's limit, the config's and Graph's own."""
        return max(1, min(limit, self._config.page_size, GRAPH_MAX_PAGE_SIZE))

    def _folder_page_size(self) -> int:
        """One page of folders. No engine limit applies — labels are read whole."""
        return max(1, min(self._config.page_size, GRAPH_MAX_PAGE_SIZE))

    async def _stream(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        """One request per message, in the order they were asked for.

        Sequential on purpose: the engine already runs several of these streams
        at once behind a semaphore, and adding a second layer of concurrency
        here would put Graph's per-mailbox throttling out of reach of the only
        knob that controls it.
        """
        for ref in refs:
            identifier = quote(ref.provider_message_id, safe="")
            body = await self._client.get_bytes(
                f"{self._mailbox}/messages/{identifier}/$value"
            )
            yield mapping.raw_message(ref, body)
