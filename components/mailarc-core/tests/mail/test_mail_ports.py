"""The port, exercised through a fake — the shape the adapters have to copy.

`MailSourcePort` is the one abstraction this project allows, which makes a
written-down reference implementation worth more than a type annotation: the
Gmail adapter, the IMAP adapter and the engine's own fake all have to agree on
the call shape, and `fetch_raw` in particular is easy to get wrong.

Note how `fetch_raw` is called below. Section 7.1 spells it ``async def ... ->
AsyncIterator[RawMessage]``, so it is a coroutine that *returns* a stream:
``async for raw in await source.fetch_raw(refs)``. An adapter therefore hands
back an async generator rather than being one.
"""

from collections.abc import AsyncIterator, Sequence

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
from mailarc_core.mail.ports import MailSourceFactory, MailSourcePort

MESSAGES = {
    "m1": b"From: alice@example.com\nTo: bob@example.com\nSubject: eins\n\nHallo.\n",
    "m2": b"From: bob@example.com\nTo: alice@example.com\nSubject: zwei\n\nDanke.\n",
}


class FakeMailSource:
    """An in-memory mailbox. Pages one message at a time to exercise the cursor."""

    provider = MailProvider.FAKE

    def __init__(self, address: str = "alice@example.com") -> None:
        self._address = address
        self.closed = False

    async def verify(self) -> AccountIdentity:
        return AccountIdentity(
            provider=self.provider, address=EmailAddress(address=self._address)
        )

    async def list_labels(self) -> Sequence[LabelInfo]:
        return [
            LabelInfo(provider_label_id="INBOX", name="INBOX", kind=LabelKind.SYSTEM)
        ]

    async def list_messages(
        self, cursor: SyncCursor | None, *, limit: int
    ) -> MessagePage:
        ids = list(MESSAGES)
        start = ids.index(cursor.token) if cursor else 0
        page = ids[start : start + limit]
        following = start + len(page)
        return MessagePage(
            refs=tuple(MessageRef(provider_message_id=id_) for id_ in page),
            next_cursor=(
                SyncCursor(
                    provider=self.provider,
                    token=ids[following],
                    kind=SyncCursorKind.FULL,
                )
                if following < len(ids)
                else None
            ),
            estimated_total=len(ids),
        )

    async def fetch_raw(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        return self._stream(refs)

    async def aclose(self) -> None:
        self.closed = True

    async def _stream(self, refs: Sequence[MessageRef]) -> AsyncIterator[RawMessage]:
        for ref in refs:
            yield RawMessage(ref=ref, raw=MESSAGES[ref.provider_message_id])


async def _drain(source: MailSourcePort) -> list[str]:
    """Walk a whole mailbox through nothing but the port."""
    subjects: list[str] = []
    cursor: SyncCursor | None = None
    while True:
        page = await source.list_messages(cursor, limit=1)
        async for raw in await source.fetch_raw(page.refs):
            subjects.append(raw.raw.split(b"Subject: ")[1].split(b"\n")[0].decode())
        cursor = page.next_cursor
        if cursor is None:
            return subjects


async def test_a_fake_source_satisfies_the_port() -> None:
    source: MailSourcePort = FakeMailSource()

    identity = await source.verify()

    assert identity.provider is MailProvider.FAKE
    assert identity.address.address == "alice@example.com"


async def test_the_engine_can_walk_a_mailbox_through_the_port_alone() -> None:
    """No provider vocabulary anywhere in `_drain` — that is the whole point."""
    source: MailSourcePort = FakeMailSource()

    assert await _drain(source) == ["eins", "zwei"]


async def test_paging_ends_when_there_is_no_next_cursor() -> None:
    source: MailSourcePort = FakeMailSource()

    page = await source.list_messages(None, limit=99)

    assert len(page.refs) == 2
    assert page.next_cursor is None
    assert page.estimated_total == 2


async def test_closing_is_part_of_the_port() -> None:
    source = FakeMailSource()

    await source.aclose()

    assert source.closed is True


def test_the_factory_is_a_callable_not_a_protocol() -> None:
    """A one-method interface around a callable would buy nothing."""
    factory: MailSourceFactory = lambda account, secret: FakeMailSource(  # noqa: E731
        address=f"{account}+{secret}@example.com"
    )

    source = factory("acct-1", "token")

    assert source.provider is MailProvider.FAKE
