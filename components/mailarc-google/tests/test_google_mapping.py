"""The anti-corruption layer, checked field by field.

Two kinds of bug live here and neither shows up as a crash. A Gmail field that
is read into the wrong domain field puts a wrong node in the graph and looks
fine forever; the base64 alphabet is worse still, because standard base64
decodes Gmail's `raw` without complaining right up to the first message that
happens to contain a `-` or a `_` — and then produces bytes that parse, store
and hash as a real message.

So the payloads below are the shapes Gmail really sends, and the decoder is
tested against a fixture that would survive the wrong alphabet silently.
"""

import base64

import pytest

from mailarc_core.mail.errors import MailPermanentError
from mailarc_core.mail.model import LabelKind, MailProvider, SyncCursorKind
from mailarc_google.source import mapping

MESSAGE = b"From: Anna <anna@example.com>\r\nSubject: Hallo\r\n\r\nText.\r\n"

LISTING = {
    "messages": [
        {"id": "18c1", "threadId": "18c0"},
        {"id": "18c2", "threadId": "18c2"},
    ],
    "nextPageToken": "07495424",
    "resultSizeEstimate": 201,
}

START_HISTORY_ID = "884411"
"""The `historyId` a delta starts from — `getProfile`'s, or a page before."""

NEXT_HISTORY_PAGE = "h-page-2"
"""Gmail's `nextPageToken` for a history walk, which is not a `startHistoryId`."""

HISTORY = {
    "history": [
        {
            "id": "884412",
            "messages": [{"id": "18c4", "threadId": "18c0"}],
            "messagesAdded": [
                {
                    "message": {
                        "id": "18c4",
                        "threadId": "18c0",
                        "labelIds": ["INBOX", "UNREAD"],
                    }
                }
            ],
        },
        {
            "id": "884413",
            "messagesAdded": [{"message": {"id": "18c5", "threadId": "18c5"}}],
        },
    ],
    "historyId": "884414",
}
"""A `users.history.list` reply for `historyTypes=messageAdded`.

Two new messages in two records, and the reply's own `historyId` beside them —
present because Gmail sends it, unused because the watermark does not come from
here.
"""

FETCHED = {
    "id": "18c1",
    "threadId": "18c0",
    "labelIds": ["INBOX", "Label_12"],
    "sizeEstimate": 5133,
    "historyId": "884411",
    "snippet": "Text.",
}


def encoded(raw: bytes = MESSAGE) -> str:
    """What Gmail puts in `raw`: base64url, and unpadded as it often arrives."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class TestTheProfile:
    def test_it_says_whose_mailbox_this_is(self) -> None:
        identity = mapping.account_identity({"emailAddress": "Jens@Example.COM"})

        assert identity.provider is MailProvider.GMAIL
        assert identity.address.address == "jens@example.com"

    def test_the_address_doubles_as_the_account_id(self) -> None:
        """Gmail has no other handle: `users/me` resolves to exactly this."""
        identity = mapping.account_identity({"emailAddress": "jens@example.com"})

        assert identity.provider_account_id == "jens@example.com"

    def test_a_profile_without_an_address_is_not_a_profile(self) -> None:
        with pytest.raises(MailPermanentError, match="emailAddress"):
            mapping.account_identity({"messagesTotal": 12})


class TestLabels:
    def test_gmails_two_kinds_both_arrive(self) -> None:
        both = mapping.labels(
            {
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "system"},
                    {"id": "Label_12", "name": "Rechnungen", "type": "user"},
                ]
            }
        )

        assert [one.kind for one in both] == [LabelKind.SYSTEM, LabelKind.USER]
        assert [one.name for one in both] == ["INBOX", "Rechnungen"]

    def test_a_kind_gmail_does_not_have_yet_reads_as_a_user_label(self) -> None:
        label = mapping.label_info({"id": "Label_9", "name": "New", "type": "smart"})

        assert label.kind is LabelKind.USER

    def test_a_count_comes_through_when_the_endpoint_sent_one(self) -> None:
        """`labels.list` omits it; `labels.get` sends it."""
        with_count = mapping.label_info({"id": "INBOX", "messagesTotal": 4212})
        without = mapping.label_info({"id": "INBOX"})

        assert with_count.message_count == 4212
        assert without.message_count is None

    def test_a_nameless_label_falls_back_to_its_id(self) -> None:
        assert mapping.label_info({"id": "Label_12"}).name == "Label_12"

    def test_a_label_without_an_id_is_no_label(self) -> None:
        with pytest.raises(MailPermanentError, match="id"):
            mapping.label_info({"name": "Rechnungen"})

    def test_an_account_can_have_none(self) -> None:
        assert mapping.labels({}) == ()


class TestTheListing:
    def test_the_page_token_is_sealed_into_the_cursor(self) -> None:
        """The engine never looks inside it — that is what makes it opaque."""
        page = mapping.message_page(LISTING)

        assert page.next_cursor is not None
        assert page.next_cursor.token == "07495424"  # noqa: S105 - a page token
        assert page.next_cursor.provider is MailProvider.GMAIL
        assert page.next_cursor.kind is SyncCursorKind.FULL

    def test_the_references_keep_their_thread(self) -> None:
        page = mapping.message_page(LISTING)

        assert [one.provider_message_id for one in page.refs] == ["18c1", "18c2"]
        assert page.refs[0].provider_thread_id == "18c0"

    def test_the_estimate_drives_the_progress_bar(self) -> None:
        assert mapping.message_page(LISTING).estimated_total == 201

    def test_no_next_token_means_the_walk_is_over(self) -> None:
        page = mapping.message_page({"messages": [{"id": "18c1"}]})

        assert page.next_cursor is None
        assert page.refs[0].provider_thread_id is None

    def test_an_empty_mailbox_lists_nothing_rather_than_failing(self) -> None:
        page = mapping.message_page({"resultSizeEstimate": 0})

        assert page.refs == ()
        assert page.next_cursor is None

    def test_an_unreadable_estimate_is_not_worth_a_failed_page(self) -> None:
        assert (
            mapping.message_page({"resultSizeEstimate": "many"}).estimated_total is None
        )


class TestTheWatermark:
    def test_the_profiles_history_id_is_where_a_delta_starts(self) -> None:
        watermark = mapping.account_watermark(
            {"emailAddress": "jens@example.com", "historyId": START_HISTORY_ID}
        )

        assert watermark.token == START_HISTORY_ID
        assert watermark.kind is SyncCursorKind.INCREMENTAL
        assert watermark.provider is MailProvider.GMAIL

    def test_a_profile_without_one_is_malformed_and_not_a_mailbox_without_a_delta(
        self,
    ) -> None:
        """`None` here would contradict the descriptor and sync nothing forever."""
        with pytest.raises(MailPermanentError, match="historyId"):
            mapping.account_watermark({"emailAddress": "jens@example.com"})


class TestTheHistoryCursor:
    def test_both_halves_survive_one_opaque_token(self) -> None:
        """`history.list` needs a start *and* a page; `SyncCursor` has one field."""
        cursor = mapping.history_cursor(START_HISTORY_ID, NEXT_HISTORY_PAGE)

        assert mapping.read_history_cursor(cursor) == (
            START_HISTORY_ID,
            NEXT_HISTORY_PAGE,
        )

    def test_a_watermark_is_a_start_with_no_page_yet(self) -> None:
        cursor = mapping.history_cursor(START_HISTORY_ID)

        assert cursor.token == START_HISTORY_ID
        assert mapping.read_history_cursor(cursor) == (START_HISTORY_ID, None)

    def test_an_empty_page_token_is_no_page_token(self) -> None:
        """`history_page` passes what Gmail sent, and Gmail may send nothing."""
        assert mapping.history_cursor(START_HISTORY_ID, "").token == START_HISTORY_ID


class TestTheHistoryPage:
    def test_the_added_messages_become_the_page(self) -> None:
        page = mapping.history_page(HISTORY, start_history_id=START_HISTORY_ID)

        assert [one.provider_message_id for one in page.refs] == ["18c4", "18c5"]
        assert page.refs[0].provider_thread_id == "18c0"
        assert page.refs[0].labels == ("INBOX", "UNREAD")

    def test_one_message_in_several_records_is_still_one_message(self) -> None:
        """Gmail lists an id again for every change it took part in."""
        twice = {
            "history": [
                {"id": "884412", "messagesAdded": [{"message": {"id": "18c4"}}]},
                {"id": "884413", "messagesAdded": [{"message": {"id": "18c4"}}]},
            ]
        }

        page = mapping.history_page(twice, start_history_id=START_HISTORY_ID)

        assert [one.provider_message_id for one in page.refs] == ["18c4"]

    def test_the_next_page_carries_the_same_start_and_gmails_token(self) -> None:
        """`startHistoryId` is required on *every* call, not only the first."""
        page = mapping.history_page(
            HISTORY | {"nextPageToken": NEXT_HISTORY_PAGE},
            start_history_id=START_HISTORY_ID,
        )

        assert page.next_cursor is not None
        assert mapping.read_history_cursor(page.next_cursor) == (
            START_HISTORY_ID,
            NEXT_HISTORY_PAGE,
        )
        assert page.next_cursor.kind is SyncCursorKind.INCREMENTAL

    def test_the_last_page_ends_the_walk_rather_than_naming_the_new_history_id(
        self,
    ) -> None:
        """A cursor that is never `None` is an engine loop that never breaks.

        The reply's own `historyId` is right there and deliberately unused: the
        point a later delta resumes from is `watermark()`, read before the walk
        and therefore behind everything it fetched.
        """
        page = mapping.history_page(HISTORY, start_history_id=START_HISTORY_ID)

        assert page.next_cursor is None

    def test_a_quiet_mailbox_reports_nothing_new_rather_than_failing(self) -> None:
        page = mapping.history_page(
            {"historyId": "884411"}, start_history_id=START_HISTORY_ID
        )

        assert page.refs == ()
        assert page.next_cursor is None
        assert page.estimated_total == 0

    def test_the_estimate_is_the_pages_own_size(self) -> None:
        """History sends no `resultSizeEstimate`, and `None` would leave the
        progress row showing the total of the last full import."""
        assert mapping.history_page(
            HISTORY, start_history_id=START_HISTORY_ID
        ).estimated_total

    def test_a_record_that_is_not_an_object_is_a_page_worth_failing(self) -> None:
        """No silent skip: a dropped record is new mail nothing would fetch."""
        with pytest.raises(MailPermanentError, match="history record"):
            mapping.history_page(
                {"history": ["884412"]}, start_history_id=START_HISTORY_ID
            )

    def test_a_change_without_a_message_is_the_same_kind_of_broken(self) -> None:
        with pytest.raises(MailPermanentError, match="message"):
            mapping.history_page(
                {"history": [{"id": "884412", "messagesAdded": [{}]}]},
                start_history_id=START_HISTORY_ID,
            )

    def test_a_record_with_only_changes_we_did_not_ask_for_adds_nothing(self) -> None:
        """`labelsAdded` arrives on records that also carry a `messagesAdded`."""
        page = mapping.history_page(
            {
                "history": [
                    {"id": "884412", "labelsAdded": [{"message": {"id": "18c4"}}]}
                ]
            },
            start_history_id=START_HISTORY_ID,
        )

        assert page.refs == ()


class TestTheFetchedMessage:
    def test_the_labels_come_from_the_fetch_and_not_from_the_listing(self) -> None:
        """This reply is the richer one, and labels are what the graph hangs on."""
        message = mapping.raw_message(FETCHED | {"raw": encoded()})

        assert message.ref.labels == ("INBOX", "Label_12")
        assert message.ref.provider_thread_id == "18c0"
        assert message.ref.size_estimate == 5133

    def test_the_bytes_come_back_exactly_as_they_were_sent(self) -> None:
        message = mapping.raw_message(FETCHED | {"raw": encoded()})

        assert message.raw == MESSAGE

    def test_the_alphabet_is_base64url_and_not_base64(self) -> None:
        """The one-in-sixty bug: standard base64 *discards* `-` and `_`."""
        awkward = bytes([0xFB, 0xFF, 0xBF]) + MESSAGE
        payload = base64.urlsafe_b64encode(awkward).decode()
        assert "-" in payload, "the fixture must hit the difference"
        assert "_" in payload, "and both halves of it"

        message = mapping.raw_message({"id": "18c1", "raw": payload})

        assert message.raw == awkward
        assert base64.b64decode(payload) != awkward, "which is what silently corrupts"

    def test_missing_padding_is_ours_to_put_back(self) -> None:
        payload = base64.urlsafe_b64encode(b"four").decode().rstrip("=")
        assert not payload.endswith("=")

        assert mapping.raw_message({"id": "18c1", "raw": payload}).raw == b"four"

    def test_a_message_without_bytes_is_one_to_skip_and_write_down(self) -> None:
        with pytest.raises(MailPermanentError, match="raw"):
            mapping.raw_message(FETCHED)

    def test_a_raw_field_that_is_not_base64url_is_skipped_too(self) -> None:
        """A truncated field: five characters can never be a base64 block."""
        with pytest.raises(MailPermanentError, match="base64url"):
            mapping.raw_message({"id": "18c1", "raw": "AAAAA"})

    def test_a_raw_field_that_is_not_even_ascii_is_skipped(self) -> None:
        with pytest.raises(MailPermanentError, match="base64url"):
            mapping.raw_message({"id": "18c1", "raw": "Grüße"})
