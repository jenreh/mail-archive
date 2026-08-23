"""IMAP as :class:`~mailarc_core.mail.ports.MailSourcePort` — six methods, no more.

The sibling of :class:`~mailarc_google.source.source.GmailSource` and of
:class:`~mailarc_sync.engine.fake.FakeMailSource`, and shaped like both on
purpose: a class attribute for the provider, a ``create`` that is the
:data:`~mailarc_core.mail.ports.MailSourceFactory`, a ``using`` that binds a
config for the composition root, a descriptor, and ``fetch_raw`` as a coroutine
that *returns* a stream. Three implementations that look alike are what make
the port a port.

Messages always come back as the RFC 5322 bytes the server stored, fetched with
``BODY.PEEK[]``. One stdlib parser in :mod:`mailarc_core.mail.parsing` then
serves every provider, and the bytes are what get hashed for ``eml_sha256`` and
what go to the blob store — so a parser fix can be replayed over the whole
archive without asking anybody's mail server again.

This class owns no state beyond its client. Which account, whose password and
how far the last run got are rows in SQLite (§8.1); ``UIDVALIDITY`` and the
resume UID travel inside the cursor the engine hands back, never in an
attribute here, because an attribute would make a run resumed by a second
worker silently list the wrong window.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from mailarc_core.mail.errors import MailCursorExpired, MailPermanentError
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
from mailarc_imap.source import mapping
from mailarc_imap.source.client import ImapClient
from mailarc_imap.source.config import ImapConfig
from mailarc_imap.source.credentials import ImapCredentials
from mailarc_imap.source.model import IMAP_DESCRIPTOR, FolderState

logger = logging.getLogger(__name__)


class ImapSource:
    """One IMAP folder, behind the six methods the engine knows."""

    provider = MailProvider.IMAP
    DESCRIPTOR = IMAP_DESCRIPTOR

    def __init__(
        self, credentials: ImapCredentials, config: ImapConfig | None = None
    ) -> None:
        self._config = config or ImapConfig()
        self._client = ImapClient(credentials, self._config)

    @classmethod
    def create(cls, account: Any, secret: str) -> MailSourcePort:
        """The :data:`~mailarc_core.mail.ports.MailSourceFactory` for IMAP.

        ``secret`` is the decrypted ``mail_credentials.secret``, which for this
        provider is what the account form wrote: JSON over
        :data:`~mailarc_imap.source.model.IMAP_DESCRIPTOR`'s own fields, every
        value a string. The account row is unused — the host, the username and
        the folder are all in the secret, because all three are things the user
        typed into the same form.

        Configuration comes from the environment. A composition root that
        builds its own :class:`ImapConfig` registers :meth:`using` instead.
        """
        return cls.using(ImapConfig())(account, secret)

    @classmethod
    def using(cls, config: ImapConfig) -> MailSourceFactory:
        """A factory bound to one configuration, for the composition root.

        ``app/composition.py`` is the only module allowed to build a component
        from configuration, and the factory signature has no room for one — so
        the config is closed over here rather than looked up later.
        """

        def build(account: Any, secret: str) -> MailSourcePort:
            return cls(ImapCredentials.from_secret(secret), config)

        return build

    @property
    def credentials(self) -> ImapCredentials:
        """The credentials as they stand. For IMAP, as they were handed over.

        Exposed even though nothing rotates: ``app/worker.py`` looks for
        ``source.credentials.to_secret()`` at the end of every run and writes it
        back when it changed. Answering the question with a value that never
        changes costs one comparison and keeps this provider out of that
        method's special cases.
        """
        return self._client.credentials

    async def verify(self) -> AccountIdentity:
        """Log in and open the folder — the two things that can be wrong.

        Both, not just the login. The folder is a credential field like the
        password, and a typo in it is the failure a person is most likely to
        make when adding an account; catching it here means "Verify" says so
        while they are still looking at the form, instead of the first
        scheduled run failing at three in the morning.

        There is no ``getProfile`` to ask afterwards — IMAP has no command for
        "who am I" — so the identity is assembled from the credentials that
        just worked (:func:`~mailarc_imap.source.mapping.identity`).
        """
        state = await self._client.select()
        logger.debug(
            "Opened %s with UIDVALIDITY %d and %d messages",
            state.folder,
            state.uidvalidity,
            state.exists,
        )
        return mapping.identity(self._client.credentials)

    async def list_labels(self) -> Sequence[LabelInfo]:
        """Every selectable folder on the server, as a label of kind ``FOLDER``.

        The whole server, not just the one folder this account syncs: the
        engine reads this once per run to give a name to whatever a message
        carries, and a person looking at the account wants to see the list they
        picked their folder from. Nothing here decides what gets imported —
        :meth:`list_messages` does, and it only ever looks at one folder.
        """
        return mapping.labels(await self._client.list_folders())

    async def list_messages(
        self, cursor: SyncCursor | None, *, limit: int
    ) -> MessagePage:
        """One page of UIDs; ``None`` starts at the bottom of the folder.

        Every call re-opens the folder first, which is what makes the
        ``UIDVALIDITY`` comparison below possible at all — the number is a
        property of the folder as it is *now*, and a cached one could not
        notice the thing this method exists to notice.

        The cursor's ``kind`` decides what a mismatch means, and it is the only
        thing that can: the engine hands back whatever it was given, so a
        cursor minted by :meth:`watermark` keeps the run incremental and a page
        token from a full walk keeps it full.
        """
        state = await self._client.select()
        kind, first_uid = self._resume_at(cursor, state)
        uids = await self._client.search_from(first_uid)
        return mapping.message_page(
            state, uids, limit=self._page_size(limit), kind=kind
        )

    async def watermark(self) -> SyncCursor | None:
        """The folder's ``UIDVALIDITY`` and ``UIDNEXT`` — where a delta starts.

        Never ``None``, which is what
        :data:`~mailarc_imap.source.model.IMAP_DESCRIPTOR` promises with
        ``supports_incremental``.

        ``UIDNEXT`` is the honest mark and the reason IMAP can have a real
        delta at all: the server promises it is the UID the *next* arrival will
        be given, and that UIDs only ever increase within a ``UIDVALIDITY``. So
        nothing can arrive below this number after it was read — which is the
        property the port's docstring asks for and the one
        ``FakeMailSource.watermark`` explicitly cannot provide over a directory
        of file names.

        Read *before* the first listing by the engine and stored only once the
        run is over, so it sits behind everything the run went on to fetch and
        the next delta overlaps rather than leaving a gap. The overlap costs
        listing calls and never a re-import: the archived-messages ledger
        filters it.
        """
        state = await self._client.select()
        return mapping.cursor(
            state.uidvalidity, state.uidnext, SyncCursorKind.INCREMENTAL
        )

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        """The RFC 5322 bytes for those references, as they arrive.

        A coroutine returning a stream, not an async generator: the engine
        calls it as ``async for raw in await source.fetch_raw(refs)``, and an
        async generator would make that ``await`` fail at the call site.
        """
        return self._stream(refs)

    async def aclose(self) -> None:
        """Log out and drop the connection. Safe to call twice."""
        await self._client.aclose()

    def _resume_at(
        self, cursor: SyncCursor | None, state: FolderState
    ) -> tuple[SyncCursorKind, int]:
        """Which walk this is and which UID it starts at.

        The one place the phase's central rule is applied. A stored
        ``UIDVALIDITY`` that no longer matches the folder's means the server
        renumbered the mailbox: every UID the archive holds now belongs to a
        different message, so the cursor is not merely behind, it is
        meaningless. RFC 3501 §2.3.1.1 says a client that sees this must
        discard everything it cached about that folder.

        What to do about it depends on which walk asked, and the difference is
        not cosmetic:

        *A delta* has nowhere to fall back to — it was only ever going to look
        at UIDs above a mark that no longer exists — so it raises
        :class:`~mailarc_core.mail.errors.MailCursorExpired`, the one error
        that means "throw the cursor away and walk everything". ``ImportEngine``
        catches it, clears the checkpoint and restarts the run as a full walk.
        Anything else would be wrong in a way nobody could see: a
        :class:`~mailarc_core.mail.errors.MailPermanentError` would be filed as
        one skipped message that never existed, and the account would quietly
        stop syncing while every run reported success.

        *A full walk* is already doing what that error asks for, so it simply
        starts again from the first UID. Raising there would be a loop: the
        engine re-raises ``MailCursorExpired`` when ``mode is FULL`` precisely
        because "start over" is no longer an available answer, and the job
        would fail every time it was retried.

        An unreadable token — a cursor from another provider left in the column
        by an account that changed type — takes the same two paths, because the
        situation is the same one: there is no position to resume from.
        """
        if cursor is None:
            return SyncCursorKind.FULL, mapping.FIRST_UID
        position = mapping.read_cursor(cursor)
        if position is not None and position.uidvalidity == state.uidvalidity:
            return cursor.kind, position.next_uid
        if cursor.kind is SyncCursorKind.INCREMENTAL:
            raise MailCursorExpired(
                f"{state.folder} was renumbered — the stored cursor {cursor.token!r} "
                f"does not belong to UIDVALIDITY {state.uidvalidity}"
            )
        logger.warning(
            "Cursor %r no longer matches UIDVALIDITY %d of %s; "
            "restarting the walk at the first UID",
            cursor.token,
            state.uidvalidity,
            state.folder,
        )
        return SyncCursorKind.FULL, mapping.FIRST_UID

    def _page_size(self, limit: int) -> int:
        """The smaller of what the engine asked for and what the config allows."""
        return max(1, min(limit, self._config.page_size))

    async def _stream(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        """One ``UID FETCH`` per reference, in the order they were asked for.

        Sequential, and not only for the reason Gmail's stream is: the engine
        already runs several of these at once behind a semaphore (§7.3), and
        underneath all of them there is **one socket**, serialised by the
        client's lock. Adding concurrency here would add contention and nothing
        else.

        The generation check happens once, before the first fetch. A UID is
        only a message while the ``UIDVALIDITY`` it was issued under still
        stands; fetching under a new one would hand the archive some other
        message's bytes filed under this message's id, which is the one failure
        in this component that no later run could detect or repair.
        """
        state = await self._client.current_folder()
        for ref in refs:
            address = mapping.read_message_id(ref)
            if address.uidvalidity != state.uidvalidity:
                raise MailPermanentError(
                    f"{state.folder} was renumbered mid-run: {ref.provider_message_id!r} "
                    f"was listed under UIDVALIDITY {address.uidvalidity}, "
                    f"the folder is now at {state.uidvalidity}"
                )
            body = await self._client.fetch_body(address.uid)
            yield mapping.raw_message(ref, body)
