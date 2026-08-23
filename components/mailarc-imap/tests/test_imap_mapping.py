"""The cursor and the message id: two formats, one home each.

Both are minted and read in :mod:`mailarc_imap.source.mapping` and nowhere
else, which is the only thing that keeps a separator from drifting away from
the split that reads it. So both round trips are pinned here, together with the
two shapes that are not round trips at all — a token from another provider, and
a folder name with the separator inside it.

Pure functions throughout: nothing in this file opens a socket.
"""

import pytest

from mailarc_core.mail.errors import MailPermanentError
from mailarc_core.mail.model import (
    LabelKind,
    MailProvider,
    MessageRef,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_imap.source import ImapCredentials, mapping
from mailarc_imap.source.model import FetchedBody, FolderListing, FolderState

INBOX = FolderState(folder="INBOX", uidvalidity=1738, uidnext=4201, exists=17)

ACCOUNT = ImapCredentials(
    host="imap.mail.me.com", username="jens@icloud.com", password="app-password"
)
"""A credential built here rather than taken from a fixture: this file has no server."""


class TestTheCursor:
    """``UIDVALIDITY`` and the first UID not yet looked at."""

    @pytest.mark.parametrize("kind", [SyncCursorKind.FULL, SyncCursorKind.INCREMENTAL])
    def test_it_round_trips(self, kind: SyncCursorKind) -> None:
        minted = mapping.cursor(1738, 4201, kind)

        position = mapping.read_cursor(minted)

        assert position is not None
        assert (position.uidvalidity, position.next_uid) == (1738, 4201)
        assert minted.kind is kind
        assert minted.provider is MailProvider.IMAP

    def test_a_token_with_no_separator_is_not_one(self) -> None:
        """A Gmail ``historyId`` in the column of an account that changed type."""
        stray = SyncCursor(
            provider=MailProvider.IMAP,
            token="918273",
            kind=SyncCursorKind.INCREMENTAL,
        )

        assert mapping.read_cursor(stray) is None

    def test_a_token_that_is_not_two_numbers_is_not_one(self) -> None:
        stray = SyncCursor(provider=MailProvider.IMAP, token="latest:page")

        assert mapping.read_cursor(stray) is None

    def test_a_uid_below_one_is_lifted_to_the_first(self) -> None:
        """UIDs start at 1; a stored zero would search a range no server issues."""
        position = mapping.read_cursor(
            SyncCursor(provider=MailProvider.IMAP, token="1738:0")
        )

        assert position is not None
        assert position.next_uid == mapping.FIRST_UID


class TestTheMessageId:
    """Folder, generation and UID — and why all three are in there."""

    def test_it_round_trips(self) -> None:
        ref = mapping.message_ref(INBOX, 4200)

        address = mapping.read_message_id(ref)

        assert (address.folder, address.uidvalidity, address.uid) == (
            "INBOX",
            1738,
            4200,
        )

    def test_a_folder_name_may_contain_the_separator(self) -> None:
        """Read from the right, so ``Notes: 2024`` does not become a UID of 2024."""
        folder = FolderState(folder="Notes: 2024", uidvalidity=7, uidnext=2)

        address = mapping.read_message_id(mapping.message_ref(folder, 1))

        assert (address.folder, address.uidvalidity, address.uid) == (
            "Notes: 2024",
            7,
            1,
        )

    def test_the_generation_is_part_of_it(self) -> None:
        """A renumbered folder must not reuse an id the ledger already knows."""
        before = mapping.message_ref(INBOX, 12)
        after = mapping.message_ref(INBOX.model_copy(update={"uidvalidity": 2}), 12)

        assert before.provider_message_id != after.provider_message_id

    def test_the_folder_is_part_of_it(self) -> None:
        """Two folders on one server routinely share a ``UIDVALIDITY``."""
        inbox = mapping.message_ref(INBOX, 12)
        archive = mapping.message_ref(
            INBOX.model_copy(update={"folder": "Archive"}), 12
        )

        assert inbox.provider_message_id != archive.provider_message_id

    @pytest.mark.parametrize(
        "identifier", ["18a4c3f", "INBOX", "INBOX:notanumber:1", "INBOX:1:x"]
    )
    def test_something_this_adapter_did_not_mint_is_permanent(
        self, identifier: str
    ) -> None:
        with pytest.raises(MailPermanentError):
            mapping.read_message_id(MessageRef(provider_message_id=identifier))


class TestThePage:
    """Paging is the adapter's business; the engine only asks for a next cursor."""

    def test_a_short_page_closes_the_walk(self) -> None:
        page = mapping.message_page(
            INBOX, (1, 2, 3), limit=10, kind=SyncCursorKind.FULL
        )

        assert len(page.refs) == 3
        assert page.next_cursor is None

    def test_a_full_page_names_the_uid_after_the_last_one(self) -> None:
        page = mapping.message_page(INBOX, (1, 2, 3), limit=2, kind=SyncCursorKind.FULL)

        assert page.next_cursor is not None
        assert page.next_cursor.token == "1738:3"  # noqa: S105 - a cursor

    def test_an_exactly_full_page_still_closes_the_walk(self) -> None:
        """Nothing left over means nothing to resume, however tidy the arithmetic."""
        page = mapping.message_page(INBOX, (1, 2), limit=2, kind=SyncCursorKind.FULL)

        assert page.next_cursor is None

    def test_an_empty_answer_closes_it_too(self) -> None:
        page = mapping.message_page(INBOX, (), limit=10, kind=SyncCursorKind.FULL)

        assert page.refs == ()
        assert page.next_cursor is None

    def test_the_kind_survives_the_page(self) -> None:
        page = mapping.message_page(
            INBOX, (1, 2, 3), limit=1, kind=SyncCursorKind.INCREMENTAL
        )

        assert page.next_cursor is not None
        assert page.next_cursor.kind is SyncCursorKind.INCREMENTAL

    def test_the_estimate_is_what_is_left_from_here(self) -> None:
        page = mapping.message_page(
            INBOX, (1, 2, 3, 4), limit=2, kind=SyncCursorKind.FULL
        )

        assert page.estimated_total == 4


class TestTheLabels:
    """A folder is a label of kind ``FOLDER`` — the domain has the member for it."""

    def test_a_selectable_folder_becomes_a_label(self) -> None:
        labels = mapping.labels((FolderListing(name="Archive", delimiter="/"),))

        assert labels[0].name == "Archive"
        assert labels[0].provider_label_id == "Archive"
        assert labels[0].kind is LabelKind.FOLDER

    def test_a_noselect_container_is_dropped(self) -> None:
        listings = (
            FolderListing(name="[Gmail]", flags=(rb"\Noselect",)),
            FolderListing(name="[Gmail]/All Mail"),
        )

        assert [label.name for label in mapping.labels(listings)] == [
            "[Gmail]/All Mail"
        ]

    def test_the_flag_is_matched_case_insensitively(self) -> None:
        """IMAP flag names are case-insensitive, and servers disagree about case."""
        listings = (FolderListing(name="Container", flags=(rb"\NOSELECT",)),)

        assert mapping.labels(listings) == ()

    def test_no_count_is_claimed(self) -> None:
        """It would cost a ``STATUS`` per folder for a number nothing acts on."""
        labels = mapping.labels((FolderListing(name="INBOX"),))

        assert labels[0].message_count is None


class TestTheIdentity:
    """IMAP has no ``getProfile``; the authenticated username is the answer."""

    def test_it_names_the_user_and_the_host(self) -> None:
        identity = mapping.identity(ACCOUNT)

        assert identity.provider is MailProvider.IMAP
        assert identity.address.address == ACCOUNT.username
        assert identity.provider_account_id == (f"{ACCOUNT.username}@{ACCOUNT.host}")

    def test_a_username_is_normalised_the_way_every_address_is(self) -> None:
        shouting = ACCOUNT.model_copy(update={"username": "JENS@Example.INVALID"})

        assert mapping.identity(shouting).address.address == "jens@example.invalid"

    def test_no_display_name_is_invented(self) -> None:
        """The field names a person, and ``LOGIN`` reports none.

        The folder is the tempting thing to put here and the wrong one: it
        would render as a human called ``INBOX`` the first time an identity
        reached a page.
        """
        assert mapping.identity(ACCOUNT).display_name is None


class TestTheRawMessage:
    """The bytes, under the reference the listing already handed the engine."""

    def test_the_reference_is_carried_over(self) -> None:
        ref = mapping.message_ref(INBOX, 9)

        raw = mapping.raw_message(ref, FetchedBody(uid=9, raw=b"bytes", size=5))

        assert raw.ref.provider_message_id == ref.provider_message_id
        assert raw.raw == b"bytes"

    def test_the_size_the_server_reported_wins_over_the_byte_count(self) -> None:
        """They disagree on servers that store bare-LF line endings."""
        ref = mapping.message_ref(INBOX, 9)

        raw = mapping.raw_message(ref, FetchedBody(uid=9, raw=b"bytes", size=7))

        assert raw.ref.size_estimate == 7
