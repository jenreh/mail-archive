"""The one seam where a second implementation is allowed to exist.

Gmail, IMAP, Microsoft Graph and the in-memory fake the engine tests against
all answer the same five questions, and the engine must not be able to tell
which one it is holding. Everything else in this project is straight layering
— there is no ``CredentialStore`` port, no factory protocol, no repository
interface, because each of those has exactly one implementation and a port
around one implementation is indirection, not architecture.

Async because a first import is tens of thousands of HTTP requests. The runic
side stays synchronous and is reached through ``asyncio.to_thread``: runic's
``AsyncSession`` cannot lazy-load and raises ``LazyLoadError`` where the
blocking one simply works.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Protocol

from mailarc_core.mail.model import (
    AccountIdentity,
    LabelInfo,
    MailProvider,
    MessagePage,
    MessageRef,
    RawMessage,
    SyncCursor,
)


class MailSourcePort(Protocol):
    """One mailbox, as far as the engine is concerned.

    Every method may raise from the taxonomy in
    :mod:`~mailarc_core.mail.errors` and nothing else: an adapter that lets an
    ``httpx`` error through has not decided whether the engine should retry.
    """

    provider: MailProvider

    async def verify(self) -> AccountIdentity:
        """Prove the credentials work and report whose mailbox this is."""
        ...

    async def list_labels(self) -> Sequence[LabelInfo]:
        """Every label or folder the account has."""
        ...

    async def list_messages(
        self, cursor: SyncCursor | None, *, limit: int
    ) -> MessagePage:
        """One page of message references; ``None`` starts from the beginning.

        Paging is the adapter's business. The engine only asks whether the
        page came back with a next cursor.
        """
        ...

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        """The RFC 5322 bytes for those references, as they arrive.

        A stream rather than a list: a batch of full messages is tens of
        megabytes, and the writer can start before the last one lands.
        """
        ...

    async def aclose(self) -> None:
        """Release the connection or the HTTP client. Safe to call twice."""
        ...


type MailSourceFactory = Callable[[Any, str], MailSourcePort]
"""Builds a source from an account row and its decrypted secret.

An alias, not a ``Protocol``: the registry needs a callable, and a one-method
interface around a callable buys nothing. The first argument is the
``MailAccountEntity`` for the account, kept untyped here so the domain does
not have to import the persistence layer for a signature.
"""
