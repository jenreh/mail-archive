"""A mailbox made of files — the second implementation that keeps the port real.

An abstraction with one implementation is a guess. This is the other one: a
directory of ``.eml`` files, walked through exactly the same six methods Gmail
will be walked through, which is what makes :class:`MailSourcePort` a port
rather than a description of Gmail. It does deltas too, in the only way a
folder honestly can: it accounts for nothing in advance, lists everything, and
leaves the archive's ledger to say what is new (:meth:`FakeMailSource.watermark`
has the reasoning). That keeps the engine's incremental mode testable without a
mailbox — and keeps a provider from being the only thing that could ever prove
it works.

It lives in ``src/`` and not in a test folder because it is a real provider —
``app/composition.py`` registers it like any other, and importing a mailbox
someone exported from Thunderbird is a genuine use of it. The credential *is*
the directory path; a path that is not there is a bad credential and says so
with :class:`MailAuthError`, the same way a rejected token would.

Paging is one page at a time from a sorted file list, so a run over it exercises
the cursor rather than getting everything in one call.
"""

import asyncio
import logging
from bisect import bisect_left, bisect_right
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from mailarc_core.mail.errors import MailAuthError, MailPermanentError
from mailarc_core.mail.model import (
    AccountIdentity,
    CredentialField,
    EmailAddress,
    LabelInfo,
    LabelKind,
    MailProvider,
    MessagePage,
    MessageRef,
    ProviderDescriptor,
    RawMessage,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_core.mail.ports import MailSourcePort

logger = logging.getLogger(__name__)

INBOX = "INBOX"
"""The one label a folder of files can honestly claim to have."""

DESCRIPTOR = ProviderDescriptor(
    provider=MailProvider.FAKE,
    label="Folder of .eml files",
    credential_fields=(
        CredentialField(
            name="directory",
            label="Directory",
            placeholder="/path/to/exported/messages",
        ),
    ),
    supports_incremental=True,
)


class FakeMailSource:
    """One directory of ``.eml`` files, behind :class:`MailSourcePort`."""

    provider = MailProvider.FAKE

    def __init__(
        self, directory: Path, *, address: str = "fixtures@example.invalid"
    ) -> None:
        self._directory = directory
        self._address = address
        self.closed = False

    @classmethod
    def create(cls, account: Any, secret: str) -> MailSourcePort:
        """The :data:`~mailarc_core.mail.ports.MailSourceFactory` for this source.

        ``secret`` is the directory path. A fake needs no credential, but it
        goes through the same encrypted column as everyone else's, so the
        registration in the composition root looks like every other one.
        """
        return cls(Path(secret), address=getattr(account, "email_address", ""))

    async def verify(self) -> AccountIdentity:
        """Prove the directory is there and report the configured address."""
        self._files()
        return AccountIdentity(
            provider=self.provider,
            address=EmailAddress(address=self._address),
            display_name=self._directory.name,
        )

    async def list_labels(self) -> Sequence[LabelInfo]:
        """Files have no labels; every message is in the inbox."""
        return [
            LabelInfo(
                provider_label_id=INBOX,
                name=INBOX,
                kind=LabelKind.SYSTEM,
                message_count=len(self._files()),
            )
        ]

    async def list_messages(
        self, cursor: SyncCursor | None, *, limit: int
    ) -> MessagePage:
        """One page of files, in name order, starting where the cursor says.

        Two readings of one token, and which one applies is the cursor's
        ``kind``. A full walk's cursor names the *next* file to hand over, so
        it resumes **at** it. A delta's cursor names the last file already
        accounted for — that is what a watermark is — so it resumes **after**
        it. One meaning per kind, and never a name that means both.

        Either way the resume point is a *bisection* rather than a lookup: a
        cursor naming a file that has since been deleted or renamed lands on
        the next one that still exists instead of silently restarting the
        mailbox from the top. Nothing would be duplicated either way — the
        engine filters what it has already archived — but a restart that
        reports itself as a resume is the kind of thing that is only ever
        noticed as an unexplained hour of listing.
        """
        files = self._files()
        names = [path.stem for path in files]
        delta = cursor is not None and cursor.kind is SyncCursorKind.INCREMENTAL
        if cursor is None:
            start = 0
        elif delta:
            start = bisect_right(names, cursor.token)
        else:
            start = bisect_left(names, cursor.token)
            self._warn_if_gone(cursor, names, start)
        page = files[start : start + limit]
        following = start + len(page)
        return MessagePage(
            refs=tuple(
                MessageRef(provider_message_id=path.stem, labels=(INBOX,))
                for path in page
            ),
            next_cursor=(
                self._continuation(names, following, delta=delta)
                if following < len(names)
                else None
            ),
            estimated_total=len(files),
        )

    async def watermark(self) -> SyncCursor | None:
        """Nothing accounted for in advance: the empty string, which sorts first.

        So a delta over a directory lists all of it, and the engine's ledger
        decides what is actually new. That is the only reading that cannot lose
        a file, and it is affordable here for the reason it would not be for
        Gmail: listing is a local ``glob`` rather than forty thousand HTTP
        requests, and nothing is fetched, parsed or written for a name the
        archive already has.

        The obvious alternative — the newest file name, everything after it is
        new — was here first and is wrong. It reads a directory as if its names
        were an arrival order, and they are not: an exported folder is named
        after subjects, after dates in whatever format the exporter chose, or
        after provider ids. Any file whose name sorts before the mark would be
        invisible to that delta *and to every delta after it*, because a
        watermark only ever moves forward. Silent, permanent, and impossible to
        notice from a job row that says "succeeded, 0 new".

        Deliberately not the newest modification time either: ``st_mtime_ns``
        depends on the filesystem's clock granularity, and two files written in
        the same tick would make a delta drop one of them. A sidecar journal
        would be honest and is machinery a folder of fixtures should not need.

        Never ``None``, which is what :data:`DESCRIPTOR`'s
        ``supports_incremental`` promises: a folder really can be swept for new
        mail, it just cannot answer "what changed since" with anything narrower
        than "look again". The directory is still checked, so a mailbox that
        has gone away says so here rather than at the next listing.
        """
        self._files()
        return SyncCursor(
            provider=self.provider,
            token="",
            kind=SyncCursorKind.INCREMENTAL,
        )

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        """The bytes of those files, one at a time.

        A coroutine returning a stream, not an async generator: that is what
        §7.1 spells, and an adapter that gets it wrong fails at the call site
        rather than silently never being iterated.
        """
        return self._stream(refs)

    async def aclose(self) -> None:
        """Nothing to release. Recorded, so a caller can prove it was called."""
        self.closed = True

    async def _stream(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        for ref in refs:
            path = self._directory / f"{ref.provider_message_id}.eml"
            try:
                raw = await asyncio.to_thread(path.read_bytes)
            except OSError as error:
                # A file that vanished between listing and fetching is gone for
                # good, which is exactly what a provider's 404 means.
                raise MailPermanentError(f"cannot read {path.name}: {error}") from error
            yield RawMessage(ref=ref, raw=raw)

    def _continuation(
        self, names: Sequence[str], following: int, *, delta: bool
    ) -> SyncCursor:
        """The cursor for the next page, spelled the way its kind reads it.

        A delta hands back the last name it delivered, a full walk the first
        one it has not — the two conventions of :meth:`list_messages`, minted
        in the one place that knows both.
        """
        return (
            SyncCursor(
                provider=self.provider,
                token=names[following - 1],
                kind=SyncCursorKind.INCREMENTAL,
            )
            if delta
            else SyncCursor(
                provider=self.provider,
                token=names[following],
                kind=SyncCursorKind.FULL,
            )
        )

    def _warn_if_gone(
        self, cursor: SyncCursor, names: Sequence[str], start: int
    ) -> None:
        """Say so when a full walk's resume point is no longer in the mailbox.

        Only for a full walk. A watermark naming a file that has since been
        deleted is the normal case, not a lost resume point, and warning about
        it every interval would train a reader to ignore the line.
        """
        if start >= len(names) or names[start] != cursor.token:
            logger.warning(
                "Cursor %r is gone from %s; resuming at the next file",
                cursor.token,
                self._directory,
            )

    def _files(self) -> list[Path]:
        """Every fixture, sorted, so paging is stable across calls."""
        if not self._directory.is_dir():
            raise MailAuthError(f"no such mailbox directory: {self._directory}")
        return sorted(self._directory.glob("*.eml"))
