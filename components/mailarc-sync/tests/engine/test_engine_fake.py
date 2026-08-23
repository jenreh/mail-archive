"""The second implementation of the port, checked against the port's own shape.

`FakeMailSource` is what turns `MailSourcePort` from a description of Gmail
into an abstraction, so the tests that matter here are the ones a Gmail adapter
will have to pass as well: paging that ends, a stream rather than a list, and
every failure expressed in the taxonomy instead of as whatever the underlying
library threw — for a directory that means `OSError` never escapes.

Since phase 7 it does deltas as well, and the two classes at the bottom are
where that is pinned down: sorted file names give a directory the one thing a
watermark needs, an order.
"""

from pathlib import Path

import pytest

from mailarc_core.mail.errors import MailAuthError, MailPermanentError
from mailarc_core.mail.model import (
    LabelKind,
    MailProvider,
    MessageRef,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_core.mail.ports import MailSourceFactory, MailSourcePort
from mailarc_sync.engine.fake import DESCRIPTOR, INBOX, FakeMailSource

MESSAGE = b"From: anna@example.com\nTo: jens@example.com\nSubject: %s\n\nHallo.\n"


@pytest.fixture
def mailbox(tmp_path) -> Path:
    directory = tmp_path / "mailbox"
    directory.mkdir()
    for name in ("a", "b", "c"):
        (directory / f"{name}.eml").write_bytes(MESSAGE % name.encode())
    return directory


async def drain(source: MailSourcePort, *, limit: int) -> list[str]:
    """Walk a whole mailbox through nothing but the port."""
    seen: list[str] = []
    cursor: SyncCursor | None = None
    while True:
        page = await source.list_messages(cursor, limit=limit)
        async for raw in await source.fetch_raw(page.refs):
            seen.append(raw.ref.provider_message_id)
        cursor = page.next_cursor
        if cursor is None:
            return seen


async def test_a_directory_satisfies_the_port(mailbox) -> None:
    source: MailSourcePort = FakeMailSource(mailbox, address="jens@example.com")

    identity = await source.verify()

    assert identity.provider is MailProvider.FAKE
    assert identity.address.address == "jens@example.com"


async def test_the_whole_mailbox_comes_back_one_page_at_a_time(mailbox) -> None:
    source = FakeMailSource(mailbox)

    assert await drain(source, limit=2) == ["a", "b", "c"]


async def test_paging_ends_when_there_is_no_next_cursor(mailbox) -> None:
    source = FakeMailSource(mailbox)

    page = await source.list_messages(None, limit=99)

    assert [ref.provider_message_id for ref in page.refs] == ["a", "b", "c"]
    assert page.next_cursor is None
    assert page.estimated_total == 3


async def test_the_cursor_names_the_next_file(mailbox) -> None:
    source = FakeMailSource(mailbox)

    page = await source.list_messages(None, limit=1)

    assert page.next_cursor is not None
    assert page.next_cursor.token == "b"  # noqa: S105 - a file name, not a secret


async def test_every_message_is_in_the_inbox_and_nowhere_else(mailbox) -> None:
    """Files have no labels; claiming otherwise would be inventing metadata."""
    source = FakeMailSource(mailbox)

    labels = await source.list_labels()
    page = await source.list_messages(None, limit=1)

    assert [one.name for one in labels] == [INBOX]
    assert labels[0].kind is LabelKind.SYSTEM
    assert labels[0].message_count == 3
    assert page.refs[0].labels == (INBOX,)


async def test_the_bytes_come_back_exactly_as_they_are_on_disk(mailbox) -> None:
    source = FakeMailSource(mailbox)

    page = await source.list_messages(None, limit=1)
    [raw] = [one async for one in await source.fetch_raw(page.refs)]

    assert raw.raw == MESSAGE % b"a"


async def test_closing_is_part_of_the_port(mailbox) -> None:
    source = FakeMailSource(mailbox)

    await source.aclose()

    assert source.closed is True


class TestFailuresSpeakTheTaxonomy:
    async def test_a_missing_directory_is_a_bad_credential(self, tmp_path) -> None:
        """The path *is* the credential here, so a wrong one is an auth error."""
        source = FakeMailSource(tmp_path / "nowhere")

        with pytest.raises(MailAuthError):
            await source.verify()

    async def test_a_file_that_vanished_is_permanent_not_transient(
        self, mailbox
    ) -> None:
        """Deleted between listing and fetching — retrying will not bring it back."""
        source = FakeMailSource(mailbox)
        ref = MessageRef(provider_message_id="gone")

        with pytest.raises(MailPermanentError):
            [one async for one in await source.fetch_raw([ref])]


class TestTheRegistration:
    def test_the_descriptor_is_the_fake_provider(self) -> None:
        assert DESCRIPTOR.provider is MailProvider.FAKE
        assert DESCRIPTOR.supports_incremental is True

    def test_it_asks_for_the_one_thing_it_needs(self) -> None:
        assert [one.name for one in DESCRIPTOR.credential_fields] == ["directory"]

    def test_create_is_a_mail_source_factory(self, mailbox) -> None:
        """What `app/composition.py` hands the registry."""

        class Account:
            email_address = "jens@example.com"

        factory: MailSourceFactory = FakeMailSource.create

        source = factory(Account(), str(mailbox))

        assert source.provider is MailProvider.FAKE


class TestResumingFromACursor:
    """A resume that quietly turns into a restart is the worst of both.

    Nothing would be duplicated — the engine filters what it already has — so
    the only symptom on a real mailbox is an unexplained extra listing pass.
    """

    async def test_a_cursor_resumes_where_it_pointed(self, mailbox) -> None:
        source = FakeMailSource(mailbox)
        first = await source.list_messages(None, limit=1)

        second = await source.list_messages(first.next_cursor, limit=2)

        assert [ref.provider_message_id for ref in second.refs] == ["b", "c"]

    async def test_a_cursor_whose_file_is_gone_resumes_at_the_next_one(
        self, mailbox
    ) -> None:
        source = FakeMailSource(mailbox)
        cursor = (await source.list_messages(None, limit=1)).next_cursor
        assert cursor is not None
        assert cursor.token == "b"  # noqa: S105 - a file name, not a secret
        (mailbox / "b.eml").unlink()

        page = await source.list_messages(cursor, limit=10)

        assert [ref.provider_message_id for ref in page.refs] == ["c"], (
            "the mailbox restarted from the top instead of resuming"
        )
        assert page.next_cursor is None

    async def test_a_cursor_past_the_end_yields_nothing(self, mailbox) -> None:
        """Every file after the cursor is gone — the walk is simply over."""
        source = FakeMailSource(mailbox)
        cursor = SyncCursor(
            provider=MailProvider.FAKE, token="z", kind=SyncCursorKind.FULL
        )

        page = await source.list_messages(cursor, limit=10)

        assert page.refs == ()
        assert page.next_cursor is None


class TestTheWatermark:
    """The sixth method, and the half of the delta the port had to grow for.

    The pairing below is the point: a descriptor that promises deltas while
    `watermark()` answers `None` is a mailbox that schedules incremental runs
    forever and never fetches anything, and nothing else in the system would
    ever notice.
    """

    async def test_the_descriptor_and_the_watermark_agree(self, mailbox) -> None:
        source = FakeMailSource(mailbox)

        watermark = await source.watermark()

        assert DESCRIPTOR.supports_incremental is (watermark is not None)

    async def test_it_accounts_for_nothing_whatever_is_in_the_directory(
        self, mailbox
    ) -> None:
        """The empty string sorts first, so every file is always in the delta.

        Not the newest name. A directory keeps no arrival log, so the only
        thing "everything after the newest name" would be is an assumption
        about how the exporter named the files.
        """
        source = FakeMailSource(mailbox)

        watermark = await source.watermark()

        assert watermark is not None
        assert watermark.token == ""
        assert watermark.kind is SyncCursorKind.INCREMENTAL

    async def test_an_empty_mailbox_watermarks_the_same_way(self, tmp_path) -> None:
        """And that is why the engine has to tell an empty token from a missing one."""
        empty = tmp_path / "empty"
        empty.mkdir()

        watermark = await FakeMailSource(empty).watermark()

        assert watermark is not None
        assert watermark.token == ""

    async def test_a_file_named_before_the_others_is_still_in_the_next_delta(
        self, mailbox
    ) -> None:
        """The bug a newest-name watermark has, and it never heals.

        Names in an exported folder are subjects, dates in whatever format, or
        provider ids — nothing that new arrivals respect. A watermark of "c"
        would hide "a2" from this delta and from every delta after it, because
        a watermark only ever moves forward.
        """
        source = FakeMailSource(mailbox)
        watermark = await source.watermark()
        (mailbox / "a2.eml").write_bytes(MESSAGE % b"a2")

        page = await source.list_messages(watermark, limit=10)

        assert "a2" in [ref.provider_message_id for ref in page.refs]

    async def test_a_missing_directory_is_still_a_bad_credential(
        self, tmp_path
    ) -> None:
        with pytest.raises(MailAuthError):
            await FakeMailSource(tmp_path / "nowhere").watermark()


class TestListingADelta:
    """An incremental cursor names what is *done*, a full one what is next.

    One token, two readings, and the `kind` is what says which. Getting it
    backwards costs either the watermark file every time or one message every
    page, and neither shows up as an error.
    """

    async def test_a_delta_from_the_watermark_lists_the_whole_directory(
        self, mailbox
    ) -> None:
        """New file or not, wherever its name sorts. The ledger picks it out.

        The cost is one `glob` and one batched `SELECT` per page; nothing is
        fetched, parsed or written for a name the archive already has.
        """
        source = FakeMailSource(mailbox)
        watermark = await source.watermark()
        (mailbox / "b2.eml").write_bytes(MESSAGE % b"b2")

        page = await source.list_messages(watermark, limit=10)

        assert [ref.provider_message_id for ref in page.refs] == [
            "a",
            "b",
            "b2",
            "c",
        ]
        assert page.next_cursor is None

    async def test_a_delta_over_an_unchanged_directory_lists_it_again(
        self, mailbox
    ) -> None:
        """Which the engine turns into nothing fetched — see `TestADelta`."""
        source = FakeMailSource(mailbox)

        page = await source.list_messages(await source.watermark(), limit=10)

        assert [ref.provider_message_id for ref in page.refs] == ["a", "b", "c"]
        assert page.next_cursor is None

    async def test_a_paged_delta_hands_back_the_last_name_it_delivered(
        self, mailbox
    ) -> None:
        """Not the next one: the next page resumes strictly after this token."""
        source = FakeMailSource(mailbox)
        start = SyncCursor(
            provider=MailProvider.FAKE, token="", kind=SyncCursorKind.INCREMENTAL
        )

        page = await source.list_messages(start, limit=2)

        assert [ref.provider_message_id for ref in page.refs] == ["a", "b"]
        assert page.next_cursor is not None
        assert page.next_cursor.token == "b"  # noqa: S105 - a file name
        assert page.next_cursor.kind is SyncCursorKind.INCREMENTAL

    async def test_a_whole_delta_walks_every_file_exactly_once(self, mailbox) -> None:
        source = FakeMailSource(mailbox)
        start = SyncCursor(
            provider=MailProvider.FAKE, token="", kind=SyncCursorKind.INCREMENTAL
        )
        seen: list[str] = []
        cursor: SyncCursor | None = start

        while True:
            page = await source.list_messages(cursor, limit=2)
            seen.extend(ref.provider_message_id for ref in page.refs)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert seen == ["a", "b", "c"]

    async def test_a_delta_cursor_naming_a_deleted_file_says_nothing_about_it(
        self, mailbox, caplog
    ) -> None:
        """Normal, not alarming: a file being deleted between pages is a Tuesday.

        The same cursor on a *full* walk is a lost resume point and does warn —
        a line that fires every interval would be a line nobody reads.
        """
        source = FakeMailSource(mailbox)
        cursor = SyncCursor(
            provider=MailProvider.FAKE, token="b", kind=SyncCursorKind.INCREMENTAL
        )
        (mailbox / "b.eml").unlink()

        with caplog.at_level("WARNING"):
            page = await source.list_messages(cursor, limit=10)

        assert [ref.provider_message_id for ref in page.refs] == ["c"]
        assert caplog.records == []
