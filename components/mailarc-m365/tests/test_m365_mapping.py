"""Graph's JSON in, domain value objects out — and nothing else out.

The anti-corruption layer is only worth having if it is complete, so these
tests are as much about what does *not* cross as about what does: no `@odata`
key, no `wellKnownName`, no `conversationId` reaches a caller under Microsoft's
name for it.

Two decisions get their own tests because both look like details and are not: a
folder is classified by `wellKnownName` rather than by its display name, which
is what keeps a German mailbox's *Posteingang* from being filed as a user
folder; and a stored cursor that does not point at Graph is `MailCursorExpired`
rather than something the engine cannot recover from.
"""

import pytest

from mailarc_core.mail.errors import MailCursorExpired, MailPermanentError
from mailarc_core.mail.model import (
    LabelKind,
    MailProvider,
    MessageRef,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_m365.source import mapping

GRAPH = "https://graph.microsoft.com/v1.0"
NEXT = f"{GRAPH}/me/mailFolders/allitems/messages/delta?$skiptoken=abc"
DELTA = f"{GRAPH}/me/mailFolders/allitems/messages/delta?$deltatoken=xyz"


def cursor(token: str, kind: SyncCursorKind = SyncCursorKind.INCREMENTAL) -> SyncCursor:
    return SyncCursor(provider=MailProvider.M365, token=token, kind=kind)


class TestTheIdentity:
    def test_the_mail_address_wins_over_the_sign_in_name(self) -> None:
        # A UPN is a sign-in name and is routinely an alias or an
        # on-premises identity; the archive keys a person on what receives mail.
        identity = mapping.account_identity(
            {
                "id": "8f4c-object-id",
                "mail": "jens@contoso.com",
                "userPrincipalName": "jens_contoso.com#EXT#@example.onmicrosoft.com",
                "displayName": "Jens R",
            }
        )
        assert identity.address.address == "jens@contoso.com"
        assert identity.display_name == "Jens R"
        assert identity.provider is MailProvider.M365

    def test_the_object_id_is_the_stable_handle(self) -> None:
        identity = mapping.account_identity({"id": "8f4c", "mail": "a@b.test"})
        assert identity.provider_account_id == "8f4c"

    def test_a_mailbox_with_no_mail_property_falls_back_to_the_upn(self) -> None:
        identity = mapping.account_identity(
            {"id": "8f4c", "userPrincipalName": "a@b.test"}
        )
        assert identity.address.address == "a@b.test"

    def test_a_reply_naming_nobody_is_a_skipped_record_not_a_blank_account(
        self,
    ) -> None:
        with pytest.raises(MailPermanentError, match="carries none of"):
            mapping.account_identity({"id": "8f4c"})

    def test_app_only_reports_the_mailbox_it_was_configured_with(self) -> None:
        identity = mapping.configured_identity("team@contoso.com")
        assert identity.address.address == "team@contoso.com"
        assert identity.provider_account_id == "team@contoso.com"


class TestTheFolders:
    def test_a_well_known_folder_is_the_providers_own(self) -> None:
        labels = mapping.labels(
            {
                "value": [
                    {
                        "id": "AAA=",
                        "displayName": "Posteingang",
                        "wellKnownName": "inbox",
                        "totalItemCount": 42,
                    }
                ]
            }
        )
        assert labels[0].kind is LabelKind.SYSTEM
        assert labels[0].name == "Posteingang"
        assert labels[0].message_count == 42

    def test_a_folder_a_person_made_is_a_folder(self) -> None:
        labels = mapping.labels(
            {"value": [{"id": "BBB=", "displayName": "Rechnungen"}]}
        )
        assert labels[0].kind is LabelKind.FOLDER

    def test_a_localised_display_name_does_not_decide_anything(self) -> None:
        """The German inbox and the English one must classify the same."""
        german = mapping.label_info(
            {"id": "A", "displayName": "Posteingang", "wellKnownName": "inbox"}
        )
        english = mapping.label_info(
            {"id": "A", "displayName": "Inbox", "wellKnownName": "inbox"}
        )
        assert german.kind is english.kind is LabelKind.SYSTEM

    def test_a_nameless_folder_is_named_after_its_id(self) -> None:
        assert mapping.label_info({"id": "CCC="}).name == "CCC="

    def test_an_empty_page_of_folders_is_an_empty_tuple(self) -> None:
        assert mapping.labels({"value": []}) == ()


class TestDescendingIntoFolders:
    """`GET /me/mailFolders` is the root level; Microsoft says so in as many
    words. Without the descent, a message in a subfolder reaches the graph
    labelled with a raw folder id."""

    def test_only_the_folders_that_report_children_are_worth_a_request(self) -> None:
        page = {
            "value": [
                {"id": "AAA=", "childFolderCount": 2},
                {"id": "BBB=", "childFolderCount": 0},
                {"id": "CCC="},  # a tenant that omits the property
            ]
        }
        assert mapping.folders_with_children(page) == ("AAA=",)

    def test_a_page_with_nothing_below_it_costs_no_second_call(self) -> None:
        assert mapping.folders_with_children({"value": []}) == ()

    def test_a_count_graph_sent_as_a_string_still_counts(self) -> None:
        page = {"value": [{"id": "AAA=", "childFolderCount": "3"}]}
        assert mapping.folders_with_children(page) == ("AAA=",)

    def test_an_entry_without_an_id_is_not_followed(self) -> None:
        # An id is what the path is built from; there is nothing to ask for.
        assert mapping.folders_with_children({"value": [{"childFolderCount": 1}]}) == ()

    def test_the_child_path_hangs_off_whichever_mailbox_is_being_read(self) -> None:
        assert (
            mapping.child_folders_path("/users/a%40b.test", "AAA=")
            == "/users/a%40b.test/mailFolders/AAA%3D/childFolders"
        )

    def test_a_folder_id_is_quoted_so_it_cannot_reshape_the_path(self) -> None:
        path = mapping.child_folders_path("/me", "AAMkA/DEF+GHI=")
        assert path.count("/") == 4
        assert path.endswith("/childFolders")


class TestTheReferences:
    def test_the_folder_and_the_categories_both_become_labels(self) -> None:
        ref = mapping.message_ref(
            {
                "id": "AAMkA",
                "conversationId": "AAQkA",
                "parentFolderId": "FOLDER=",
                "categories": ["Rot", "Projekt X"],
            }
        )
        assert ref.provider_message_id == "AAMkA"
        assert ref.provider_thread_id == "AAQkA"
        assert ref.labels == ("FOLDER=", "Rot", "Projekt X")

    def test_a_message_with_neither_carries_no_labels(self) -> None:
        assert mapping.message_ref({"id": "AAMkA"}).labels == ()

    def test_an_empty_category_is_not_a_label(self) -> None:
        ref = mapping.message_ref({"id": "A", "categories": ["", "  ", "Rot"]})
        assert ref.labels == ("Rot",)

    def test_a_message_without_an_id_is_a_skipped_record(self) -> None:
        with pytest.raises(MailPermanentError, match="carries no id"):
            mapping.message_ref({"conversationId": "AAQkA"})


class TestTheListing:
    def test_a_next_link_becomes_a_full_cursor_carrying_the_whole_url(self) -> None:
        page = mapping.message_page(
            {"value": [{"id": "A"}], "@odata.nextLink": f"{GRAPH}/me/messages?$skip=10"}
        )
        assert page.next_cursor is not None
        assert page.next_cursor.kind is SyncCursorKind.FULL
        assert page.next_cursor.token == f"{GRAPH}/me/messages?$skip=10"

    def test_the_last_page_ends_the_walk(self) -> None:
        assert mapping.message_page({"value": [{"id": "A"}]}).next_cursor is None

    def test_an_odata_count_is_read_when_a_deployment_asked_for_one(self) -> None:
        page = mapping.message_page({"value": [], "@odata.count": 1234})
        assert page.estimated_total == 1234

    def test_a_reply_whose_value_is_not_a_list_is_a_skipped_page(self) -> None:
        with pytest.raises(MailPermanentError, match="where a list of messages"):
            mapping.message_page({"value": {"id": "A"}})

    def test_an_entry_that_is_not_an_object_is_a_skipped_page(self) -> None:
        with pytest.raises(MailPermanentError, match="not an object"):
            mapping.message_page({"value": ["AAMkA"]})

    def test_a_counter_that_is_not_a_number_is_simply_missing(self) -> None:
        # An estimate drives a progress bar and is allowed to be absent; no
        # page is worth failing over one.
        assert (
            mapping.message_page({"value": [], "@odata.count": "many"}).estimated_total
            is None
        )
        assert (
            mapping.message_page({"value": [], "@odata.count": True}).estimated_total
            is None
        )


class TestTheDelta:
    def test_a_next_link_pages_the_delta_and_keeps_it_incremental(self) -> None:
        page = mapping.delta_page({"value": [{"id": "A"}], "@odata.nextLink": NEXT})
        assert page.next_cursor is not None
        assert page.next_cursor.kind is SyncCursorKind.INCREMENTAL
        assert page.next_cursor.token == NEXT

    def test_a_delta_link_is_not_a_next_cursor(self) -> None:
        """Following one always yields another; the page loop would never end."""
        page = mapping.delta_page({"value": [{"id": "A"}], "@odata.deltaLink": DELTA})
        assert page.next_cursor is None
        assert mapping.delta_link({"@odata.deltaLink": DELTA}) == DELTA

    def test_a_removed_entry_is_skipped_rather_than_archived_or_deleted(self) -> None:
        page = mapping.delta_page(
            {
                "value": [
                    {"id": "A", "conversationId": "T"},
                    {"id": "B", "@removed": {"reason": "deleted"}},
                ]
            }
        )
        assert [ref.provider_message_id for ref in page.refs] == ["A"]

    def test_the_page_reports_its_own_size_because_a_delta_has_no_total(self) -> None:
        page = mapping.delta_page({"value": [{"id": "A"}, {"id": "B"}]})
        assert page.estimated_total == 2

    def test_the_two_links_are_read_apart(self) -> None:
        assert mapping.next_link({"@odata.nextLink": NEXT}) == NEXT
        assert mapping.next_link({"@odata.deltaLink": DELTA}) is None
        assert mapping.delta_link({"@odata.nextLink": NEXT}) is None


class TestTheDeltaAddress:
    def test_the_delta_always_carries_a_folder_segment(self) -> None:
        """Graph has no mailbox-wide message delta; every URL names a folder."""
        assert (
            mapping.delta_path("/me", "allitems")
            == "/me/mailFolders/allitems/messages/delta"
        )

    def test_a_folder_id_is_quoted_so_it_cannot_reshape_the_path(self) -> None:
        path = mapping.delta_path("/me", "AAMkA/DEF+GHI=")
        # The id's own slash must not become another path segment.
        assert path.count("/") == 5
        assert path.endswith("/messages/delta")

    def test_the_query_a_delta_starts_with_is_the_one_it_keeps(self) -> None:
        params = mapping.delta_params()
        assert params["changeType"] == "created"
        assert params["$select"] == "id,conversationId,parentFolderId,categories"


class TestReadingACursor:
    def test_a_graph_link_comes_back_whole(self) -> None:
        assert mapping.read_cursor_url(cursor(NEXT), api_base_url=GRAPH) == NEXT

    def test_a_cursor_pointing_somewhere_else_is_thrown_away_not_followed(self) -> None:
        """Every request leaves with a bearer token on it."""
        elsewhere = "https://evil.example.test/v1.0/me/messages"
        with pytest.raises(MailCursorExpired, match=r"evil\.example\.test"):
            mapping.read_cursor_url(cursor(elsewhere), api_base_url=GRAPH)

    def test_a_bare_token_from_an_older_adapter_is_thrown_away(self) -> None:
        with pytest.raises(MailCursorExpired, match="not a Graph link"):
            mapping.read_cursor_url(cursor("918273"), api_base_url=GRAPH)

    def test_the_comparison_is_on_the_origin_and_not_on_the_path(self) -> None:
        deep = f"{GRAPH}/users/a%40b.test/mailFolders/x/messages/delta?$deltatoken=q"
        assert mapping.read_cursor_url(cursor(deep), api_base_url=GRAPH) == deep


def test_the_raw_message_keeps_the_bytes_and_the_listings_reference() -> None:
    """`$value` answers with MIME and no metadata, so the ref comes from before."""
    ref = MessageRef(provider_message_id="A", labels=("FOLDER=",))
    raw = mapping.raw_message(ref, b"From: a@b.test\r\n\r\nhi\r\n")
    assert raw.ref is ref
    assert raw.raw.startswith(b"From:")
