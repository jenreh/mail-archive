"""The six methods, and the two things about them that can lose mail.

The checklist in ``docs/developer/adding-a-provider.md`` is the floor: a happy
path for each method, the four errors, two pages with the last one closing the
walk, and a resume. On top of it, the two questions this provider answers
differently from Gmail:

**A renumbered mailbox.** ``UIDVALIDITY`` is IMAP's way of saying "every UID you
stored means something else now". That is ``MailCursorExpired`` and nothing
else — and only from the delta, because the engine re-raises it from a full
walk and a full walk has somewhere to fall back to.

**One socket, eight streams.** ``ImportEngine`` opens ``fetch_concurrency``
fetch streams against one source. IMAP has one connection with one command in
flight, so the last class here runs eight at once and checks that every message
came back under its own id.
"""

import asyncio

import pytest
from imap_server import OVERLAPPING, FakeImapServer, eml

from mailarc_core.mail.errors import (
    MailAuthError,
    MailCursorExpired,
    MailPermanentError,
)
from mailarc_core.mail.model import (
    LabelKind,
    MailProvider,
    MessageRef,
    RawMessage,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_imap.source import (
    IMAP_DESCRIPTOR,
    ImapConfig,
    ImapCredentials,
    ImapSource,
    mapping,
)


def fill(server: FakeImapServer, *uids: int, folder: str = "INBOX") -> None:
    """Put one distinguishable message under each of those UIDs."""
    for uid in uids:
        server.mailbox(folder).add(uid, eml(uid))


async def collect(source: ImapSource, refs: list[MessageRef]) -> list[RawMessage]:
    """Drain a ``fetch_raw`` stream. The ``await`` is the point of the signature."""
    return [raw async for raw in await source.fetch_raw(refs)]


class TestVerify:
    """Prove the credentials work, and report whose mailbox this is."""

    async def test_it_reports_the_authenticated_user(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        identity = await source.verify()

        assert identity.provider is MailProvider.IMAP
        assert identity.address.address == server.username
        assert identity.provider_account_id == f"{server.username}@127.0.0.1"

    async def test_a_wrong_password_is_an_auth_error(
        self, config: ImapConfig, server: FakeImapServer
    ) -> None:
        built = ImapSource(
            ImapCredentials(
                host="127.0.0.1",
                port=server.port,
                username=server.username,
                password="not-the-app-password",
            ),
            config,
        )

        with pytest.raises(MailAuthError):
            await built.verify()
        await built.aclose()

    async def test_it_proves_the_login_without_naming_a_folder(
        self, config: ImapConfig, server: FakeImapServer
    ) -> None:
        """A walk covers the whole account, so there is no folder to get wrong.

        This replaces a test that typed a folder name into the credential and
        expected ``verify`` to reject it. That failure mode is gone with the
        field: ``verify`` proves the login and lists, and a mailbox whose
        folders are not the ones somebody expected is not a credential problem.
        """
        built = ImapSource(
            ImapCredentials(
                host="127.0.0.1",
                port=server.port,
                username=server.username,
                password=server.password,
            ),
            config,
        )

        identity = await built.verify()

        assert identity.address.address == server.username.lower()
        assert not any("EXAMINE" in command for command in server.commands)
        await built.aclose()


class TestListLabels:
    """Folders, as the domain's one label kind for exactly this."""

    async def test_every_folder_comes_back_as_a_folder(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        server.mailbox("Archive")

        labels = await source.list_labels()

        assert {label.name for label in labels} == {"INBOX", "Archive"}
        assert all(label.kind is LabelKind.FOLDER for label in labels)

    async def test_the_name_is_the_id_because_imap_issues_none(
        self, source: ImapSource
    ) -> None:
        labels = await source.list_labels()

        assert all(label.provider_label_id == label.name for label in labels)

    async def test_a_container_that_cannot_be_selected_is_left_out(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """``[Gmail]`` itself is a ``\\Noselect`` node, not a mailbox."""
        server.mailbox("[Gmail]").flags = (rb"\Noselect", rb"\HasChildren")
        server.mailbox("[Gmail]/All Mail")

        names = {label.name for label in await source.list_labels()}

        assert "[Gmail]" not in names
        assert "[Gmail]/All Mail" in names

    async def test_junk_and_trash_are_reported(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """This method describes the mailbox; it does not decide what is imported."""
        server.mailbox("Junk").flags = (rb"\Junk", rb"\HasNoChildren")
        server.mailbox("Trash").flags = (rb"\Trash", rb"\HasNoChildren")

        names = {label.name for label in await source.list_labels()}

        assert {"Junk", "Trash"} <= names


class TestTheFullWalk:
    """``None`` starts at the bottom, and the last page closes the walk."""

    async def test_it_lists_the_whole_folder(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1, 2, 3)

        page = await source.list_messages(None, limit=10)

        assert len(page.refs) == 3
        assert page.next_cursor is None
        assert page.estimated_total == 3

    async def test_a_message_carries_its_folder_as_its_label(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 4)

        page = await source.list_messages(None, limit=10)

        assert page.refs[0].labels == ("INBOX",)

    async def test_no_thread_id_is_invented(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """IMAP has none, and ``parsing`` derives ``thread_hint`` from the headers."""
        fill(server, 4)

        page = await source.list_messages(None, limit=10)

        assert page.refs[0].provider_thread_id is None

    async def test_the_reference_carries_the_generation_as_well_as_the_uid(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """Otherwise a renumbered folder reuses ids the ledger already knows."""
        server.mailbox().uidvalidity = 55
        fill(server, 9)

        page = await source.list_messages(None, limit=10)

        assert page.refs[0].provider_message_id == "INBOX:55:9"

    async def test_two_pages_and_the_second_one_ends_it(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1, 2, 3, 4, 5)

        first = await source.list_messages(None, limit=3)
        assert first.next_cursor is not None
        second = await source.list_messages(first.next_cursor, limit=3)

        assert [ref.provider_message_id for ref in first.refs] == [
            "INBOX:1000:1",
            "INBOX:1000:2",
            "INBOX:1000:3",
        ]
        assert [ref.provider_message_id for ref in second.refs] == [
            "INBOX:1000:4",
            "INBOX:1000:5",
        ]
        assert second.next_cursor is None

    async def test_no_page_is_ever_handed_over_twice(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """The cursor names the UID *after* the last one delivered."""
        fill(server, 1, 2, 3, 4)

        first = await source.list_messages(None, limit=2)
        assert first.next_cursor is not None
        second = await source.list_messages(first.next_cursor, limit=2)

        assert not {ref.provider_message_id for ref in first.refs} & {
            ref.provider_message_id for ref in second.refs
        }

    async def test_the_cursor_stays_full_across_pages(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """A walk that reported itself incremental would checkpoint into the wrong scope."""
        fill(server, 1, 2, 3)

        page = await source.list_messages(None, limit=2)

        assert page.next_cursor is not None
        assert page.next_cursor.kind is SyncCursorKind.FULL

    async def test_an_empty_folder_lists_nothing_and_ends(
        self, source: ImapSource
    ) -> None:
        page = await source.list_messages(None, limit=10)

        assert page.refs == ()
        assert page.next_cursor is None

    async def test_the_config_caps_what_the_engine_asked_for(
        self, credentials: ImapCredentials, config: ImapConfig, server: FakeImapServer
    ) -> None:
        fill(server, 1, 2, 3, 4)
        built = ImapSource(credentials, config.model_copy(update={"page_size": 2}))

        page = await built.list_messages(None, limit=100)
        await built.aclose()

        assert len(page.refs) == 2


class TestTheDelta:
    """A watermark, and the mail that arrived after it."""

    async def test_the_watermark_is_never_none(self, source: ImapSource) -> None:
        assert await source.watermark() is not None

    async def test_the_descriptor_and_the_watermark_agree(
        self, source: ImapSource
    ) -> None:
        """``tests/test_composition.py`` walks the registry and asserts this too."""
        mark = await source.watermark()

        assert IMAP_DESCRIPTOR.supports_incremental is (mark is not None)

    async def test_the_watermark_is_incremental(self, source: ImapSource) -> None:
        mark = await source.watermark()

        assert mark is not None
        assert mark.kind is SyncCursorKind.INCREMENTAL
        assert mark.provider is MailProvider.IMAP

    async def test_nothing_below_the_watermark_can_arrive_afterwards(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """``UIDNEXT`` is the UID the next arrival gets. That is the whole delta."""
        fill(server, 1, 2, 3)
        mark = await source.watermark()
        assert mark is not None

        page = await source.list_messages(mark, limit=10)

        assert page.refs == ()

    async def test_it_lists_what_arrived_after_the_mark(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1, 2, 3)
        mark = await source.watermark()
        assert mark is not None
        fill(server, 4, 5)

        page = await source.list_messages(mark, limit=10)

        assert [ref.provider_message_id for ref in page.refs] == [
            "INBOX:1000:4",
            "INBOX:1000:5",
        ]

    async def test_a_delta_pages_too_and_stays_incremental(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        mark = await source.watermark()
        assert mark is not None
        fill(server, 1, 2, 3, 4)

        first = await source.list_messages(mark, limit=2)
        assert first.next_cursor is not None
        assert first.next_cursor.kind is SyncCursorKind.INCREMENTAL
        second = await source.list_messages(first.next_cursor, limit=2)

        assert len(first.refs) == 2
        assert len(second.refs) == 2
        assert second.next_cursor is None

    async def test_a_quiet_mailbox_does_not_keep_handing_back_its_last_message(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """The ``n:*`` range quirk, seen from the port rather than from the client."""
        fill(server, 1, 2, 3)
        mark = await source.watermark()
        assert mark is not None

        for _ in range(3):
            page = await source.list_messages(mark, limit=10)
            assert page.refs == ()


class TestARenumberedMailbox:
    """``UIDVALIDITY`` changed: every stored UID means something else now."""

    async def test_a_delta_says_the_cursor_expired(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1, 2)
        mark = await source.watermark()
        assert mark is not None
        server.mailbox().uidvalidity = 2000

        with pytest.raises(MailCursorExpired):
            await source.list_messages(mark, limit=10)

    async def test_it_is_not_a_permanent_error(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """The engine's per-message handler would file it as a skipped message."""
        mark = await source.watermark()
        assert mark is not None
        server.mailbox().uidvalidity = 2000

        with pytest.raises(MailCursorExpired) as raised:
            await source.list_messages(mark, limit=10)

        assert not isinstance(raised.value, MailPermanentError)

    async def test_a_full_walk_starts_over_instead_of_raising(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """``ImportEngine`` re-raises this one from a full walk, so it must not raise."""
        fill(server, 1, 2, 3)
        first = await source.list_messages(None, limit=2)
        assert first.next_cursor is not None
        server.mailbox().uidvalidity = 2000

        page = await source.list_messages(first.next_cursor, limit=10)

        assert [ref.provider_message_id for ref in page.refs] == [
            "INBOX:2000:1",
            "INBOX:2000:2",
            "INBOX:2000:3",
        ]

    async def test_a_full_walk_says_so_in_the_log(
        self, source: ImapSource, server: FakeImapServer, caplog
    ) -> None:
        fill(server, 1)
        first = await source.list_messages(None, limit=10)
        server.mailbox().uidvalidity = 2000

        with caplog.at_level("WARNING"):
            await source.list_messages(
                SyncCursor(
                    provider=MailProvider.IMAP,
                    token='{"at": "INBOX", "marks": {"INBOX": [1000, 1]}, "v": 2}',
                    kind=SyncCursorKind.FULL,
                ),
                limit=10,
            )

        assert "walking it from the top" in caplog.text
        assert first.refs

    async def test_a_cursor_from_another_provider_is_treated_the_same_way(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """An account re-added as IMAP can have a Gmail ``historyId`` in the column."""
        fill(server, 1)
        gmail = SyncCursor(
            provider=MailProvider.IMAP,
            token="918273",
            kind=SyncCursorKind.INCREMENTAL,
        )

        with pytest.raises(MailCursorExpired):
            await source.list_messages(gmail, limit=10)

    async def test_a_full_walk_survives_an_unreadable_token(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """The other half of the pair above, and the half that must not raise.

        ``ImportEngine`` re-raises ``MailCursorExpired`` when the mode is
        ``FULL``, so a walk resuming from a token this adapter cannot read has
        to start over rather than say so — otherwise the job fails identically
        on every retry and the account never syncs again.
        """
        fill(server, 1, 2)
        stray = SyncCursor(
            provider=MailProvider.IMAP,
            token="918273",
            kind=SyncCursorKind.FULL,
        )

        page = await source.list_messages(stray, limit=10)

        assert [ref.provider_message_id for ref in page.refs] == [
            "INBOX:1000:1",
            "INBOX:1000:2",
        ]


class TestFetchRaw:
    """The bytes, unaltered, under the reference the listing handed over."""

    async def test_it_yields_the_stored_bytes(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1, 2)
        page = await source.list_messages(None, limit=10)

        fetched = await collect(source, list(page.refs))

        assert [raw.raw for raw in fetched] == [eml(1), eml(2)]

    async def test_the_reference_that_came_back_is_the_one_that_went_in(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """``ImportEngine._fetch_slice`` retries by id; a new one would loop forever."""
        fill(server, 1, 2)
        page = await source.list_messages(None, limit=10)

        fetched = await collect(source, list(page.refs))

        assert [raw.ref.provider_message_id for raw in fetched] == [
            ref.provider_message_id for ref in page.refs
        ]

    async def test_the_size_arrives_with_the_bytes(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1)
        page = await source.list_messages(None, limit=10)

        fetched = await collect(source, list(page.refs))

        assert fetched[0].ref.size_estimate == len(eml(1))

    async def test_a_uid_that_vanished_between_listing_and_fetch_is_permanent(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1, 2)
        page = await source.list_messages(None, limit=10)
        del server.mailbox().messages[2]

        with pytest.raises(MailPermanentError):
            await collect(source, list(page.refs))

    async def test_a_reference_from_a_renumbered_folder_is_refused(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """Fetching under a new generation hands the archive some other message."""
        fill(server, 1)
        page = await source.list_messages(None, limit=10)
        server.mailbox().uidvalidity = 2000
        await source.list_messages(None, limit=10)

        with pytest.raises(MailPermanentError, match="renumbered mid-run"):
            await collect(source, list(page.refs))

    async def test_a_reference_this_adapter_did_not_mint_is_refused(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1)
        await source.list_messages(None, limit=10)

        with pytest.raises(MailPermanentError, match="not an IMAP message reference"):
            await collect(source, [MessageRef(provider_message_id="18a4c3f")])

    async def test_it_is_a_coroutine_returning_a_stream(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """Not an async generator: the engine writes ``await source.fetch_raw(...)``."""
        fill(server, 1)
        page = await source.list_messages(None, limit=10)

        stream = await source.fetch_raw(list(page.refs))

        assert hasattr(stream, "__aiter__")


class TestOneSocketAndEightStreams:
    """``fetch_concurrency`` streams share one connection with one command in flight.

    Two assertions, because they fail differently and the difference matters.
    The second is the outcome — every message came back under its own id — and
    deleting the client's lock does break it, but it breaks it as a ten-second
    cascade of read timeouts on a suite that otherwise runs in under a second,
    which is a slow and easily-misread signal for a fast unit suite.

    The first asserts the invariant itself: **one command in flight**. It is the
    protocol rule the lock exists to keep, and it can be observed directly
    rather than inferred from corrupted bytes —
    :attr:`~imap_server.FakeImapServer.reply_delay` makes the server hold each
    answer back for a measurable moment and watch the socket while it does, so a
    second command sent before the first was answered is *seen* rather than
    deduced. Without the lock it fails in a fraction of a second, and it says
    what went wrong.
    """

    async def test_no_second_command_is_sent_while_one_is_unanswered(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """The lock, asserted as the protocol rule rather than as its symptom."""
        fill(server, *range(1, 17))
        page = await source.list_messages(None, limit=100)
        slices = [list(page.refs[start::8]) for start in range(8)]
        server.reply_delay = 0.02

        await asyncio.gather(*(collect(source, part) for part in slices))

        assert OVERLAPPING not in server.commands

    async def test_concurrent_streams_do_not_read_each_others_replies(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        uids = range(1, 25)
        fill(server, *uids)
        page = await source.list_messages(None, limit=100)
        slices = [list(page.refs[start::8]) for start in range(8)]

        results = await asyncio.gather(*(collect(source, part) for part in slices))

        for part, fetched in zip(slices, results, strict=True):
            assert [raw.ref.provider_message_id for raw in fetched] == [
                ref.provider_message_id for ref in part
            ]
            for raw in fetched:
                uid = int(raw.ref.provider_message_id.rsplit(":", 1)[1])
                assert raw.raw == eml(uid)


class TestTheFactories:
    """What ``app/composition.py`` registers, and what it hands the worker."""

    def test_the_descriptor_is_on_the_class(self) -> None:
        assert ImapSource.DESCRIPTOR is IMAP_DESCRIPTOR
        assert ImapSource.provider is MailProvider.IMAP

    async def test_using_binds_a_config_the_adapter_never_reads_itself(
        self, config: ImapConfig, server: FakeImapServer
    ) -> None:
        secret = ImapCredentials(
            host="127.0.0.1",
            port=server.port,
            username=server.username,
            password=server.password,
        ).to_secret()

        built = ImapSource.using(config)(object(), secret)
        identity = await built.verify()
        await built.aclose()

        assert identity.address.address == server.username

    def test_create_reads_its_configuration_from_the_environment(
        self, server: FakeImapServer
    ) -> None:
        """The registration a composition root uses when it built no config.

        Nothing is dialled: the client connects on its first command, so this
        stays a construction test and reaches no server, real or fake.
        """
        secret = ImapCredentials(
            host="127.0.0.1",
            port=server.port,
            username=server.username,
            password=server.password,
        ).to_secret()

        built = ImapSource.create(object(), secret)

        assert isinstance(built, ImapSource)
        assert built.provider is MailProvider.IMAP
        assert built.credentials.to_secret() == secret

    async def test_a_secret_that_does_not_parse_fails_at_the_factory(
        self, config: ImapConfig
    ) -> None:
        with pytest.raises(MailAuthError):
            ImapSource.using(config)(object(), "{}")

    async def test_the_credentials_are_readable_for_the_workers_rotation_check(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """``app/worker.py`` compares ``to_secret()`` to what it opened with."""
        opened_with = source.credentials.to_secret()

        await source.verify()

        assert source.credentials.to_secret() == opened_with


class TestClosing:
    async def test_twice_is_safe(self, credentials, config) -> None:
        built = ImapSource(credentials, config)
        await built.verify()

        await built.aclose()
        await built.aclose()

    async def test_closing_something_never_used_is_safe(
        self, credentials, config
    ) -> None:
        await ImapSource(credentials, config).aclose()


class TestTheWalkCoversTheWholeAccount:
    """The whole mailbox, always — there is no folder to pick any more."""

    async def test_every_folder_is_walked(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1, 2)
        fill(server, 5, folder="Reisen")
        fill(server, 9, folder="Reisen/Rechnungen")

        seen: list[str] = []
        cursor: SyncCursor | None = None
        while True:
            page = await source.list_messages(cursor, limit=10)
            seen.extend(ref.provider_message_id for ref in page.refs)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert {one.rsplit(":", 2)[0] for one in seen} == {
            "INBOX",
            "Reisen",
            "Reisen/Rechnungen",
        }
        assert len(seen) == 4

    async def test_the_folder_is_the_label_whole(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """Gmail's mechanics, chosen deliberately over splitting the path.

        A nested folder is one tag carrying its whole path, exactly as
        ``mailarc-google`` stores a nested Gmail label.
        """
        fill(server, 1, folder="Reisen/Rechnungen")

        labels: set[str] = set()
        cursor: SyncCursor | None = None
        while True:
            page = await source.list_messages(cursor, limit=10)
            labels.update(label for ref in page.refs for label in ref.labels)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert "Reisen/Rechnungen" in labels
        assert "Reisen" not in labels

    async def test_a_message_in_two_folders_keeps_two_distinct_ids(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """Two folders on one server routinely share a ``UIDVALIDITY``.

        If the id were the bare UID the ledger would recognise the second
        sighting as already archived and most of the mailbox would never be
        fetched — silent, permanent, and reported as success.
        """
        fill(server, 1)
        fill(server, 1, folder="Archive")

        ids: list[str] = []
        cursor: SyncCursor | None = None
        while True:
            page = await source.list_messages(cursor, limit=10)
            ids.extend(ref.provider_message_id for ref in page.refs)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert len(ids) == len(set(ids)) == 2

    async def test_a_resumed_walk_does_not_restart_a_finished_folder(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        fill(server, 1, 2, 3)
        fill(server, 7, folder="Reisen")

        first = await source.list_messages(None, limit=2)
        assert first.next_cursor is not None
        second = await source.list_messages(first.next_cursor, limit=2)

        delivered = [ref.provider_message_id for ref in (*first.refs, *second.refs)]
        assert len(delivered) == len(set(delivered))

    async def test_a_folder_deleted_mid_walk_carries_on_at_the_next_one(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """A restart that reports itself as a resume is only ever noticed as an
        unexplained hour of listing."""
        fill(server, 1, folder="Alpha")
        fill(server, 2, folder="Beta")
        fill(server, 3, folder="Gamma")

        first = await source.list_messages(None, limit=10)
        assert first.next_cursor is not None
        del server.folders["Beta"]

        page = await source.list_messages(first.next_cursor, limit=10)

        assert [ref.provider_message_id for ref in page.refs] == ["Gamma:1000:3"]

    async def test_the_watermark_marks_every_folder(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """A delta resumes each folder above what the full walk saw of it."""
        fill(server, 1, 2)
        fill(server, 8, folder="Reisen")

        mark = await source.watermark()

        assert mark is not None
        assert mark.kind is SyncCursorKind.INCREMENTAL
        position = mapping.read_cursor(mark)
        assert position is not None
        assert set(position.marks) == {"INBOX", "Reisen"}
        assert position.marks["INBOX"].next_uid == 3
        assert position.marks["Reisen"].next_uid == 9


class TestSpamAndDeletedAreNotArchived:
    """Spam was never the user's mail; a deleted message is one they threw away."""

    @pytest.mark.parametrize(
        ("folder", "flags"),
        [
            ("Junk", ()),
            ("Deleted Messages", ()),
            ("Aufbewahrung", (rb"\Junk",)),
            ("Papierkorb", (rb"\Trash",)),
        ],
    )
    async def test_they_are_not_walked(
        self,
        source: ImapSource,
        server: FakeImapServer,
        folder: str,
        flags: tuple[bytes, ...],
    ) -> None:
        fill(server, 1)
        fill(server, 2, folder=folder)
        server.mailbox(folder).flags = (*server.mailbox(folder).flags, *flags)

        seen: list[str] = []
        cursor: SyncCursor | None = None
        while True:
            page = await source.list_messages(cursor, limit=10)
            seen.extend(ref.provider_message_id for ref in page.refs)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert seen == ["INBOX:1000:1"]

    async def test_they_are_still_reported_as_folders(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """``list_labels`` describes the mailbox; ``list_messages`` decides what
        is kept. A folder list that silently omitted two names would be one
        nobody could trust."""
        fill(server, 2, folder="Junk")

        names = {label.name for label in await source.list_labels()}

        assert "Junk" in names

    async def test_a_user_folder_nested_under_another_is_kept(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """Somebody who files marketing under ``Kunden/Junk`` meant to keep it."""
        fill(server, 4, folder="Kunden/Junk")

        page = await source.list_messages(None, limit=10)
        seen = list(page.refs)
        while page.next_cursor is not None:
            page = await source.list_messages(page.next_cursor, limit=10)
            seen.extend(page.refs)

        assert [ref.provider_message_id for ref in seen] == ["Kunden/Junk:1000:4"]


class TestAMailboxWithNothingToWalk:
    """The empty cases, which are ordinary answers rather than failures."""

    async def test_an_account_of_nothing_but_spam_lists_nothing(
        self, source: ImapSource, server: FakeImapServer, caplog
    ) -> None:
        """And says so once, because a silent empty import is indistinguishable
        from a working one that found no mail."""
        server.folders.clear()
        fill(server, 1, folder="Junk")

        with caplog.at_level("WARNING"):
            page = await source.list_messages(None, limit=10)

        assert page.refs == ()
        assert page.next_cursor is None
        assert "no syncable folder" in caplog.text

    async def test_such_an_account_has_no_watermark_either(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """``None`` from a source with nothing to delta over. The descriptor
        promises a delta for the provider, not for an empty mailbox."""
        server.folders.clear()

        assert await source.watermark() is None

    async def test_a_walk_whose_last_folder_vanished_is_simply_over(
        self, source: ImapSource, server: FakeImapServer
    ) -> None:
        """Not an error: the page loop has nothing left to ask for.

        ``INBOX`` is cleared deliberately. The fake server always serves one,
        and with it in place the walk simply moved on to an empty inbox — so
        the assertions below held without the deleted-folder path ever running.
        """
        server.folders.clear()
        fill(server, 1, folder="Alpha")
        fill(server, 2, folder="Zulu")

        first = await source.list_messages(None, limit=10)
        assert first.next_cursor is not None
        del server.folders["Zulu"]

        page = await source.list_messages(first.next_cursor, limit=10)

        assert page.refs == ()
        assert page.next_cursor is None
