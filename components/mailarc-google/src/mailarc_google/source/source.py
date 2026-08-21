"""Gmail as :class:`~mailarc_core.mail.ports.MailSourcePort` — four calls, no more.

The sibling of :class:`~mailarc_sync.engine.fake.FakeMailSource`, and shaped
like it on purpose: a class attribute for the provider, a ``create`` that is
the :data:`~mailarc_core.mail.ports.MailSourceFactory`, a descriptor the
composition root registers, and ``fetch_raw`` as a coroutine that *returns* a
stream. Two implementations that look alike are what make the port a port.

Messages always come back with ``format=raw`` (§10, phase 3). One stdlib parser
in :mod:`mailarc_core.mail.parsing` then serves every provider, and this file
stays thin enough to be read in a sitting; ``format=full`` would mean mapping
Google's MIME tree here and a second parser to maintain. Labels, ``threadId``
and the size arrive alongside the bytes and become the reference the archive
sees.

This class owns no state beyond its client: which account, whose credentials
and how far the last run got are rows in SQLite (§8.1).
"""

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import quote

from mailarc_core.mail.model import (
    AccountIdentity,
    LabelInfo,
    MailProvider,
    MessagePage,
    MessageRef,
    RawMessage,
    SyncCursor,
)
from mailarc_core.mail.ports import MailSourceFactory, MailSourcePort
from mailarc_google.source import mapping
from mailarc_google.source.client import GmailClient
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.credentials import GmailCredentials
from mailarc_google.source.model import GMAIL_DESCRIPTOR

logger = logging.getLogger(__name__)

PROFILE_PATH = "/users/me/profile"
LABELS_PATH = "/users/me/labels"
MESSAGES_PATH = "/users/me/messages"

RAW_FORMAT = "raw"
"""The only format this adapter ever asks for. See the module docstring."""

GMAIL_MAX_PAGE_SIZE = 500
"""Gmail's own ceiling for ``maxResults``; asking for more is a 400."""


class GmailSource:
    """One Gmail account, behind the five methods the engine knows."""

    provider = MailProvider.GMAIL
    DESCRIPTOR = GMAIL_DESCRIPTOR

    def __init__(
        self, credentials: GmailCredentials, config: GmailConfig | None = None
    ) -> None:
        self._config = config or GmailConfig()
        self._client = GmailClient(credentials, self._config)

    @classmethod
    def create(cls, account: Any, secret: str) -> MailSourcePort:
        """The :data:`~mailarc_core.mail.ports.MailSourceFactory` for Gmail.

        ``secret`` is the decrypted ``mail_credentials.secret``, which for this
        provider is the JSON of a :class:`GmailCredentials`. The account row is
        unused: Gmail says whose mailbox this is itself, in :meth:`verify`.

        Configuration comes from the environment. A composition root that
        builds its own :class:`GmailConfig` registers :meth:`using` instead.
        """
        return cls.using(GmailConfig())(account, secret)

    @classmethod
    def using(cls, config: GmailConfig) -> MailSourceFactory:
        """A factory bound to one configuration, for the composition root.

        ``app/composition.py`` is the only module allowed to build a component
        from configuration, and the factory signature has no room for one — so
        the config is closed over here rather than looked up later.
        """

        def build(account: Any, secret: str) -> MailSourcePort:
            return cls(GmailCredentials.from_secret(secret), config)

        return build

    @property
    def credentials(self) -> GmailCredentials:
        """The credentials as they stand, refreshes and rotations included.

        Phase 3 item 3: a rotated refresh token has to reach
        ``mail_credentials``, and this is the only copy of it.
        """
        return self._client.credentials

    async def verify(self) -> AccountIdentity:
        """Read the profile — the one call that proves whose mailbox this is."""
        identity = mapping.account_identity(await self._client.get(PROFILE_PATH))
        logger.debug("Gmail credentials belong to %s", identity.address.address)
        return identity

    async def list_labels(self) -> Sequence[LabelInfo]:
        """Every label of the account, system and user alike.

        Read once per run by the engine, which needs it to turn the opaque
        ``Label_12`` on a message into the name a human gave it.
        """
        return mapping.labels(await self._client.get(LABELS_PATH))

    async def list_messages(
        self, cursor: SyncCursor | None, *, limit: int
    ) -> MessagePage:
        """One page of references; ``None`` starts at the top of the mailbox.

        The page size is the smallest of what the engine asked for, what
        :class:`GmailConfig` allows this adapter to lean on Gmail with (§11)
        and Gmail's own maximum. Spam and trash stay out: that is Gmail's
        default for ``messages.list`` and an archive of a mailbox is what the
        user sees in it.
        """
        params: dict[str, str | int] = {
            "maxResults": max(
                1, min(limit, self._config.page_size, GMAIL_MAX_PAGE_SIZE)
            )
        }
        if cursor is not None:
            params["pageToken"] = cursor.token
        return mapping.message_page(
            await self._client.get(MESSAGES_PATH, params=params)
        )

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

    async def _stream(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        """One request per message, in the order they were asked for.

        Sequential on purpose: the engine already runs several of these
        streams at once behind a semaphore (§7.3), and adding a second layer
        of concurrency here would put the 250 units/user/s quota out of reach
        of the only knob that controls it.
        """
        for ref in refs:
            payload = await self._client.get(
                f"{MESSAGES_PATH}/{quote(ref.provider_message_id, safe='')}",
                params={"format": RAW_FORMAT},
            )
            yield mapping.raw_message(payload)
