"""Microsoft 365 walked through nothing but the port, against a local server.

The tests that matter are the ones `FakeMailSource` already passes, because
that is what makes `MailSourcePort` an abstraction rather than a description of
one vendor: paging that ends, a stream rather than a list, labels that resolve,
and every failure expressed in the taxonomy. Whatever is Graph's alone —
`@odata.nextLink`, `$value`, a delta that lives inside a folder — is asserted
on the requests this adapter actually sends.

**Both modes go through all six methods**, because the whole claim of the
discriminated credential is that the mode changes the path and nothing else.
The one place it changes more than that is `verify()`, and that is asserted
too: an app-only run must never touch `/me`.

**No test here talks to Microsoft.** `M365Config` carries the API root and MSAL
is replaced at `refresh_async`.
"""

import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from werkzeug import Request, Response

from mailarc_core.mail.errors import (
    MailAuthError,
    MailCursorExpired,
    MailPermanentError,
)
from mailarc_core.mail.model import (
    LabelKind,
    MailProvider,
    MessageRef,
    SyncCursor,
    SyncCursorKind,
)
from mailarc_core.mail.ports import MailSourceFactory, MailSourcePort
from mailarc_m365.source import client as client_module
from mailarc_m365.source import source as source_module
from mailarc_m365.source.config import M365Config
from mailarc_m365.source.credentials import (
    M365AppOnlyCredentials,
    M365DelegatedCredentials,
)
from mailarc_m365.source.model import M365_DESCRIPTOR, M365Mode
from mailarc_m365.source.source import M365Source

API_ROOT = "/v1.0"
MAILBOX = "team@contoso.com"
TENANT = "contoso.onmicrosoft.com"
LIVE = "eyJ0.live"  # noqa: S105 - a fixture
MINTED = "eyJ0.minted"  # noqa: S105 - a fixture
STORED = "0.AR-stored"  # noqa: S105 - a fixture
ROTATED = "0.AR-rotated"  # noqa: S105 - a fixture

ME = f"{API_ROOT}/me"
USERS = f"{API_ROOT}/users/{MAILBOX}"
"""What werkzeug sees once `/users/team%40contoso.com` is decoded."""

IDENTITY = {
    "id": "8f4c-object-id",
    "mail": "jens@contoso.com",
    "userPrincipalName": "jens@contoso.com",
    "displayName": "Jens R",
}
FOLDERS = {
    "value": [
        {
            "id": "INBOX=",
            "displayName": "Posteingang",
            "wellKnownName": "inbox",
            "totalItemCount": 3,
        },
        {"id": "RECH=", "displayName": "Rechnungen", "totalItemCount": 1},
    ]
}
MAIL = {
    "AAMkA1": b"From: anna@example.com\r\nSubject: eins\r\n\r\nText.\r\n",
    "AAMkA2": b"From: bob@example.com\r\nSubject: zwei\r\n\r\nText.\r\n",
    "AAMkA3": b"From: cleo@example.com\r\nSubject: drei\r\n\r\nText.\r\n",
}


def config_for(httpserver: Any, **overrides: Any) -> M365Config:
    settings: dict[str, Any] = {
        "client_id": "a-client",
        "api_base_url": httpserver.url_for(API_ROOT),
        "authority_host": "http://127.0.0.1:1",  # never reached; MSAL is replaced
        "request_timeout": 5.0,
    } | overrides
    return M365Config(**settings)


def delegated(**overrides: Any) -> M365DelegatedCredentials:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "refresh_token": STORED,
        "access_token": LIVE,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    } | overrides
    return M365DelegatedCredentials(**fields)


def app_only(**overrides: Any) -> M365AppOnlyCredentials:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "mailbox": MAILBOX,
        "access_token": LIVE,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    } | overrides
    return M365AppOnlyCredentials(mode=M365Mode.APP_ONLY, **fields)


def source_for(
    httpserver: Any, credentials: Any = None, **overrides: Any
) -> M365Source:
    return M365Source(credentials or delegated(), config_for(httpserver, **overrides))


def json_response(body: object, status: int = 200) -> Response:
    return Response(json.dumps(body), status=status, content_type="application/json")


def refusal(code: str, status: int) -> Response:
    return json_response({"error": {"code": code, "message": code}}, status)


class Script:
    """Answers a fixed list of replies in order, repeating the last one."""

    def __init__(self, *replies: Response) -> None:
        self._replies = replies
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        reply = self._replies[min(len(self.requests), len(self._replies) - 1)]
        self.requests.append(request)
        return reply


def serve_folders(httpserver: Any, prefix: str = ME) -> None:
    httpserver.expect_request(f"{prefix}/mailFolders").respond_with_json(FOLDERS)


def serve_mail(httpserver: Any, prefix: str = ME) -> None:
    for identifier, raw in MAIL.items():
        httpserver.expect_request(
            f"{prefix}/messages/{identifier}/$value"
        ).respond_with_data(raw, content_type="text/plain")


def listing(
    *identifiers: str, next_link: str | None = None, count: int | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "value": [
            {
                "id": one,
                "conversationId": f"T-{one}",
                "parentFolderId": "INBOX=",
                "categories": [],
            }
            for one in identifiers
        ]
    }
    if next_link:
        body["@odata.nextLink"] = next_link
    if count is not None:
        body["@odata.count"] = count
    return body


class TestVerify:
    async def test_a_delegated_mailbox_says_whose_it_is(self, httpserver: Any) -> None:
        httpserver.expect_request(ME).respond_with_json(IDENTITY)
        source = source_for(httpserver)

        identity = await source.verify()
        await source.aclose()

        assert identity.provider is MailProvider.M365
        assert identity.address.address == "jens@contoso.com"
        assert identity.provider_account_id == "8f4c-object-id"

    async def test_it_asks_for_four_properties_and_not_the_whole_directory_row(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(ME).respond_with_json(IDENTITY)
        source = source_for(httpserver)

        await source.verify()
        await source.aclose()

        assert httpserver.log[0][0].args["$select"] == (
            "id,mail,userPrincipalName,displayName"
        )

    async def test_app_only_proves_the_grant_on_the_mailbox_it_was_given(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(f"{USERS}/mailFolders/inbox").respond_with_json(
            {"id": "INBOX=", "displayName": "Inbox"}
        )
        source = source_for(httpserver, app_only())

        identity = await source.verify()
        await source.aclose()

        assert identity.address.address == MAILBOX
        # `/me` does not exist for a token nobody signed in to, and asking for
        # `User.Read.All` to read `/users/{id}` would be a directory-wide grant.
        assert [request.path for request, _ in httpserver.log] == [
            f"{USERS}/mailFolders/inbox"
        ]

    async def test_a_refused_app_only_grant_is_an_auth_error(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(f"{USERS}/mailFolders/inbox").respond_with_response(
            refusal("ErrorAccessDenied", 403)
        )
        source = source_for(httpserver, app_only())

        with pytest.raises(MailAuthError):
            await source.verify()
        await source.aclose()

    async def test_a_mailbox_that_is_not_there_is_an_auth_error_not_a_skip(
        self, httpserver: Any
    ) -> None:
        """The Mailbox field names something Graph cannot open.

        Left at the client's default this would be a `MailPermanentError`,
        which is the instruction to drop one *message* — and there is no
        message here. The account has to reach `auth_error` so the page that
        holds the field offers itself again.
        """
        httpserver.expect_request(f"{USERS}/mailFolders/inbox").respond_with_response(
            refusal("ErrorItemNotFound", 404)
        )
        source = source_for(httpserver, app_only())

        with pytest.raises(MailAuthError) as raised:
            await source.verify()
        await source.aclose()

        assert not isinstance(raised.value, MailPermanentError)


class TestTheLabels:
    async def test_every_folder_comes_back_named_and_classified(
        self, httpserver: Any
    ) -> None:
        serve_folders(httpserver)
        source = source_for(httpserver)

        labels = await source.list_labels()
        await source.aclose()

        assert [one.name for one in labels] == ["Posteingang", "Rechnungen"]
        assert [one.kind for one in labels] == [LabelKind.SYSTEM, LabelKind.FOLDER]

    async def test_folders_are_paged_to_the_end(self, httpserver: Any) -> None:
        """Graph pages `mailFolders` at ten by default; Gmail's labels do not.

        Without the loop, every message in a folder past the first page would
        be archived under a bare folder id.
        """
        second = {"value": [{"id": "ARCH=", "displayName": "Archiv"}]}
        first = dict(FOLDERS) | {
            "@odata.nextLink": httpserver.url_for(f"{ME}/mailFolders")
        }
        script = Script(json_response(first), json_response(second))
        httpserver.expect_request(f"{ME}/mailFolders").respond_with_handler(script)
        source = source_for(httpserver)

        labels = await source.list_labels()
        await source.aclose()

        assert [one.provider_label_id for one in labels] == ["INBOX=", "RECH=", "ARCH="]

    async def test_app_only_reads_the_folders_of_the_named_mailbox(
        self, httpserver: Any
    ) -> None:
        serve_folders(httpserver, USERS)
        source = source_for(httpserver, app_only())

        assert len(await source.list_labels()) == 2
        await source.aclose()

    async def test_a_mailbox_that_never_stops_paging_is_bounded(
        self, httpserver: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unbounded loop over a paginated endpoint is a lost afternoon.

        The bound is on *requests*, not on pages of one collection: pages and
        descents into child folders draw on the same purse, so a mailbox that
        is deep and a mailbox that is wide cost the same ceiling.
        """
        monkeypatch.setattr(source_module, "MAX_FOLDER_PAGES", 2)
        script = Script(
            json_response(
                dict(FOLDERS)
                | {"@odata.nextLink": httpserver.url_for(f"{ME}/mailFolders")}
            )
        )
        httpserver.expect_request(f"{ME}/mailFolders").respond_with_handler(script)
        source = source_for(httpserver)

        labels = await source.list_labels()
        await source.aclose()

        assert len(script.requests) == 2
        # The same two folders came back twice; a label list with the same id
        # in it twice would be two nodes for one folder.
        assert [one.provider_label_id for one in labels] == ["INBOX=", "RECH="]

    async def test_folders_filed_inside_other_folders_are_found_too(
        self, httpserver: Any
    ) -> None:
        """`GET /me/mailFolders` is the root level and Microsoft says so.

        "This API does not return all mail folders in a mailbox; to get all
        folders, each child folder must be traversed separately." An Archive
        filed inside the Inbox is the ordinary shape of a tidy mailbox, and
        without the descent every message in one reaches the graph labelled
        with a raw folder id.
        """
        root = {
            "value": [
                {
                    "id": "INBOX=",
                    "displayName": "Posteingang",
                    "wellKnownName": "inbox",
                    "childFolderCount": 1,
                },
                {"id": "RECH=", "displayName": "Rechnungen", "childFolderCount": 0},
            ]
        }
        httpserver.expect_request(f"{ME}/mailFolders").respond_with_json(root)
        httpserver.expect_request(
            f"{ME}/mailFolders/INBOX=/childFolders"
        ).respond_with_json(
            {"value": [{"id": "ARCH=", "displayName": "Archiv", "childFolderCount": 0}]}
        )
        source = source_for(httpserver)

        labels = await source.list_labels()
        await source.aclose()

        assert [one.provider_label_id for one in labels] == ["INBOX=", "RECH=", "ARCH="]
        assert [one.name for one in labels][-1] == "Archiv"

    async def test_the_descent_goes_deeper_than_one_level(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(f"{ME}/mailFolders").respond_with_json(
            {"value": [{"id": "A", "displayName": "A", "childFolderCount": 1}]}
        )
        httpserver.expect_request(f"{ME}/mailFolders/A/childFolders").respond_with_json(
            {"value": [{"id": "B", "displayName": "B", "childFolderCount": 1}]}
        )
        httpserver.expect_request(f"{ME}/mailFolders/B/childFolders").respond_with_json(
            {"value": [{"id": "C", "displayName": "C", "childFolderCount": 0}]}
        )
        source = source_for(httpserver)

        labels = await source.list_labels()
        await source.aclose()

        assert [one.provider_label_id for one in labels] == ["A", "B", "C"]

    async def test_a_flat_mailbox_still_costs_exactly_one_request(
        self, httpserver: Any
    ) -> None:
        """The descent must not tax the mailbox that has nothing to descend."""
        script = Script(json_response(FOLDERS))
        httpserver.expect_request(f"{ME}/mailFolders").respond_with_handler(script)
        source = source_for(httpserver)

        await source.list_labels()
        await source.aclose()

        # FOLDERS reports no `childFolderCount`, which is also what a tenant
        # that omits the property looks like: it degrades to the flat listing.
        assert len(script.requests) == 1

    async def test_app_only_descends_inside_the_named_mailbox(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(f"{USERS}/mailFolders").respond_with_json(
            {"value": [{"id": "INBOX=", "displayName": "Inbox", "childFolderCount": 1}]}
        )
        httpserver.expect_request(
            f"{USERS}/mailFolders/INBOX=/childFolders"
        ).respond_with_json(
            {"value": [{"id": "ARCH=", "displayName": "Archiv", "childFolderCount": 0}]}
        )
        source = source_for(httpserver, app_only())

        labels = await source.list_labels()
        await source.aclose()

        assert len(labels) == 2
        assert [request.path for request, _ in httpserver.log] == [
            f"{USERS}/mailFolders",
            f"{USERS}/mailFolders/INBOX=/childFolders",
        ]


class TestTheFullWalk:
    async def test_the_first_page_asks_graph_for_the_newest_first(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(f"{ME}/messages").respond_with_json(listing("AAMkA1"))
        source = source_for(httpserver)

        await source.list_messages(None, limit=50)
        await source.aclose()

        query = httpserver.log[0][0].args
        assert query["$orderby"] == "receivedDateTime desc"
        assert query["$select"] == "id,conversationId,parentFolderId,categories"
        assert query["$top"] == "50"

    async def test_the_first_page_asks_for_the_mailbox_size(
        self, httpserver: Any
    ) -> None:
        """Without ``$count`` Graph sends no total and the progress bar has no
        denominator — the one walk of the four that had none."""
        httpserver.expect_request(f"{ME}/messages").respond_with_json(listing("AAMkA1"))
        source = source_for(httpserver)

        await source.list_messages(None, limit=50)
        await source.aclose()

        assert httpserver.log[0][0].args["$count"] == "true"

    async def test_the_mailbox_size_reaches_the_page(self, httpserver: Any) -> None:
        httpserver.expect_request(f"{ME}/messages").respond_with_json(
            listing("AAMkA1", count=57)
        )
        source = source_for(httpserver)

        page = await source.list_messages(None, limit=50)
        await source.aclose()

        assert page.estimated_total == 57

    async def test_a_page_without_a_count_estimates_nothing(
        self, httpserver: Any
    ) -> None:
        """``None`` rather than the page's own length: the engine reads it as
        "keep the estimate you have", and a length would report 100%."""
        httpserver.expect_request(f"{ME}/messages").respond_with_json(listing("AAMkA1"))
        source = source_for(httpserver)

        page = await source.list_messages(None, limit=50)
        await source.aclose()

        assert page.estimated_total is None

    async def test_the_page_size_is_the_smallest_of_the_three_limits(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(f"{ME}/messages").respond_with_json(listing("AAMkA1"))
        source = source_for(httpserver, page_size=25)

        await source.list_messages(None, limit=5000)
        await source.aclose()

        assert httpserver.log[0][0].args["$top"] == "25"

    async def test_two_pages_then_the_walk_ends(self, httpserver: Any) -> None:
        following = httpserver.url_for(f"{ME}/messages") + "?$skip=1"
        script = Script(
            json_response(listing("AAMkA1", next_link=following)),
            json_response(listing("AAMkA2", "AAMkA3")),
        )
        httpserver.expect_request(f"{ME}/messages").respond_with_handler(script)
        source = source_for(httpserver)

        first = await source.list_messages(None, limit=10)
        assert first.next_cursor is not None
        assert first.next_cursor.kind is SyncCursorKind.FULL

        second = await source.list_messages(first.next_cursor, limit=10)
        await source.aclose()

        assert [one.provider_message_id for one in second.refs] == ["AAMkA2", "AAMkA3"]
        assert second.next_cursor is None

    async def test_resuming_follows_the_link_graph_handed_out_untouched(
        self, httpserver: Any
    ) -> None:
        script = Script(json_response(listing("AAMkA2")))
        httpserver.expect_request(f"{ME}/messages").respond_with_handler(script)
        source = source_for(httpserver)
        resumed = SyncCursor(
            provider=MailProvider.M365,
            token=httpserver.url_for(f"{ME}/messages") + "?$skiptoken=deadbeef",
            kind=SyncCursorKind.FULL,
        )

        await source.list_messages(resumed, limit=10)
        await source.aclose()

        assert script.requests[0].args["$skiptoken"] == "deadbeef"
        # The query is baked into the link; sending `$top` again would fight it.
        assert "$top" not in script.requests[0].args

    async def test_a_cursor_pointing_away_from_graph_is_never_followed(
        self, httpserver: Any
    ) -> None:
        """No bearer token leaves for an origin that is not the configured Graph.

        The walk restarts from the top instead of raising, and that is not
        leniency — see the next test.
        """
        httpserver.expect_request(f"{ME}/messages").respond_with_json(listing("AAMkA1"))
        source = source_for(httpserver)
        forged = SyncCursor(
            provider=MailProvider.M365,
            token="https://evil.example.test/v1.0/me/messages",
            kind=SyncCursorKind.FULL,
        )

        page = await source.list_messages(forged, limit=10)
        await source.aclose()

        assert [request.host for request, _ in httpserver.log] == [
            httpserver.url_for("").split("//", 1)[1].rstrip("/")
        ]
        assert [one.provider_message_id for one in page.refs] == ["AAMkA1"]

    @pytest.mark.parametrize(
        "token", ["https://evil.example.test/v1.0/me/messages", "a-bare-page-token"]
    )
    async def test_an_unusable_full_checkpoint_restarts_the_walk_rather_than_dying(
        self, httpserver: Any, token: str
    ) -> None:
        """`MailCursorExpired` must not leave this method on a full walk.

        `ImportEngine.run` recovers from an expired cursor by falling back to a
        full walk, and for a run that already *is* one it re-raises — and it
        never clears the full checkpoint on that path. So an unusable page
        token would be read, refused and re-raised on every run for ever: an
        account that fails permanently and silently on a stale value. Starting
        the walk again is what `MailCursorExpired` asks for anyway, and the
        first page overwrites the bad row.
        """
        httpserver.expect_request(f"{ME}/messages").respond_with_json(
            listing("AAMkA1", next_link=httpserver.url_for(f"{ME}/messages") + "?p=2")
        )
        source = source_for(httpserver)
        stored = SyncCursor(
            provider=MailProvider.M365, token=token, kind=SyncCursorKind.FULL
        )

        page = await source.list_messages(stored, limit=10)
        await source.aclose()

        # Listed from the top, with the query a fresh walk sends...
        assert httpserver.log[0][0].args["$orderby"] == "receivedDateTime desc"
        # ...and handed back a cursor that replaces the unusable one.
        assert page.next_cursor is not None
        assert page.next_cursor.token != token

    async def test_an_unusable_delta_checkpoint_still_raises(
        self, httpserver: Any
    ) -> None:
        """The delta keeps the error, because there the engine's fallback works.

        `MailCursorExpired` out of an incremental listing is what makes the
        engine walk the mailbox instead. Swallowing it here the way the full
        walk does would leave a delta silently listing from nowhere.
        """
        source = source_for(httpserver)
        forged = SyncCursor(
            provider=MailProvider.M365,
            token="https://evil.example.test/v1.0/me/mailFolders/allitems",
            kind=SyncCursorKind.INCREMENTAL,
        )

        with pytest.raises(MailCursorExpired):
            await source.list_messages(forged, limit=10)
        await source.aclose()

        assert httpserver.log == []


class TestTheDelta:
    def delta_path(self, prefix: str = ME) -> str:
        return f"{prefix}/mailFolders/allitems/messages/delta"

    async def test_the_watermark_drains_the_chain_to_its_delta_link(
        self, httpserver: Any
    ) -> None:
        """A `deltaLink` is only ever handed out at the end of a chain."""
        finished = httpserver.url_for(self.delta_path()) + "?$deltatoken=final"
        script = Script(
            json_response(
                listing(
                    "AAMkA1",
                    next_link=httpserver.url_for(self.delta_path()) + "?$skiptoken=1",
                )
            ),
            json_response({"value": [], "@odata.deltaLink": finished}),
        )
        httpserver.expect_request(self.delta_path()).respond_with_handler(script)
        source = source_for(httpserver)

        mark = await source.watermark()
        await source.aclose()

        assert mark is not None
        assert mark.kind is SyncCursorKind.INCREMENTAL
        assert mark.token == finished

    async def test_the_drain_asks_for_created_changes_and_a_large_page(
        self, httpserver: Any
    ) -> None:
        script = Script(json_response({"value": [], "@odata.deltaLink": "x"}))
        httpserver.expect_request(self.delta_path()).respond_with_handler(script)
        source = source_for(httpserver, watermark_page_size=750)

        await source.watermark()
        await source.aclose()

        first = script.requests[0]
        assert first.args["changeType"] == "created"
        # The same projection the delta itself will use: Graph bakes it into
        # the link, so a leaner drain would mint a cursor that loses labels.
        assert first.args["$select"] == "id,conversationId,parentFolderId,categories"
        assert first.headers["Prefer"] == "odata.maxpagesize=750"

    async def test_the_query_is_not_repeated_on_a_link_that_carries_it(
        self, httpserver: Any
    ) -> None:
        script = Script(
            json_response(
                {
                    "value": [],
                    "@odata.nextLink": httpserver.url_for(self.delta_path())
                    + "?$skiptoken=1",
                }
            ),
            json_response({"value": [], "@odata.deltaLink": "x"}),
        )
        httpserver.expect_request(self.delta_path()).respond_with_handler(script)
        source = source_for(httpserver)

        await source.watermark()
        await source.aclose()

        assert "changeType" not in script.requests[1].args
        assert script.requests[1].args["$skiptoken"] == "1"

    async def test_a_drain_that_runs_out_of_pages_hands_back_where_it_got_to(
        self, httpserver: Any
    ) -> None:
        """A mid-chain `nextLink` is a legal cursor; the next run carries on."""
        following = httpserver.url_for(self.delta_path()) + "?$skiptoken=onward"
        httpserver.expect_request(self.delta_path()).respond_with_json(
            {"value": [], "@odata.nextLink": following}
        )
        source = source_for(httpserver, watermark_max_pages=2)

        mark = await source.watermark()
        await source.aclose()

        assert mark is not None
        assert mark.token == following

    async def test_a_delta_reply_with_neither_link_is_a_skipped_page(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(self.delta_path()).respond_with_json({"value": []})
        source = source_for(httpserver)

        with pytest.raises(MailPermanentError, match="without a deltaLink"):
            await source.watermark()
        await source.aclose()

    async def test_a_stored_delta_link_lists_only_what_changed(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(self.delta_path()).respond_with_json(
            listing("AAMkA2", "AAMkA3")
        )
        source = source_for(httpserver)
        stored = SyncCursor(
            provider=MailProvider.M365,
            token=httpserver.url_for(self.delta_path()) + "?$deltatoken=abc",
            kind=SyncCursorKind.INCREMENTAL,
        )

        page = await source.list_messages(stored, limit=10)
        await source.aclose()

        assert [one.provider_message_id for one in page.refs] == ["AAMkA2", "AAMkA3"]
        assert page.next_cursor is None

    async def test_the_delta_pages_twice_and_then_stops_at_its_delta_link(
        self, httpserver: Any
    ) -> None:
        following = httpserver.url_for(self.delta_path()) + "?$skiptoken=2"
        script = Script(
            json_response(listing("AAMkA1", next_link=following)),
            json_response(
                dict(listing("AAMkA2"))
                | {"@odata.deltaLink": httpserver.url_for(self.delta_path())}
            ),
        )
        httpserver.expect_request(self.delta_path()).respond_with_handler(script)
        source = source_for(httpserver)
        stored = SyncCursor(
            provider=MailProvider.M365,
            token=httpserver.url_for(self.delta_path()) + "?$deltatoken=abc",
            kind=SyncCursorKind.INCREMENTAL,
        )

        first = await source.list_messages(stored, limit=10)
        assert first.next_cursor is not None
        assert first.next_cursor.token == following

        last = await source.list_messages(first.next_cursor, limit=10)
        await source.aclose()

        assert [one.provider_message_id for one in last.refs] == ["AAMkA2"]
        assert last.next_cursor is None

    async def test_a_410_resync_required_throws_the_cursor_away(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(self.delta_path()).respond_with_response(
            refusal("resyncRequired", 410)
        )
        source = source_for(httpserver)
        stored = SyncCursor(
            provider=MailProvider.M365,
            token=httpserver.url_for(self.delta_path()) + "?$deltatoken=stale",
            kind=SyncCursorKind.INCREMENTAL,
        )

        with pytest.raises(MailCursorExpired):
            await source.list_messages(stored, limit=10)
        await source.aclose()

    async def test_a_410_anywhere_else_is_not_an_expired_cursor(
        self, httpserver: Any
    ) -> None:
        """Otherwise a proxy's 410 would re-walk somebody's whole mailbox."""
        httpserver.expect_request(f"{ME}/messages").respond_with_response(
            refusal("Gone", 410)
        )
        source = source_for(httpserver)

        with pytest.raises(MailPermanentError) as raised:
            await source.list_messages(None, limit=10)
        await source.aclose()

        assert not isinstance(raised.value, MailCursorExpired)

    async def test_a_404_on_the_delta_is_a_missing_folder_not_a_stale_token(
        self, httpserver: Any
    ) -> None:
        # `allitems` is not present in every mailbox; re-walking would not
        # bring the folder back, so this must not become MailCursorExpired.
        httpserver.expect_request(self.delta_path()).respond_with_response(
            refusal("ErrorItemNotFound", 404)
        )
        source = source_for(httpserver)
        stored = SyncCursor(
            provider=MailProvider.M365,
            token=httpserver.url_for(self.delta_path()),
            kind=SyncCursorKind.INCREMENTAL,
        )

        with pytest.raises(MailPermanentError) as raised:
            await source.list_messages(stored, limit=10)
        await source.aclose()

        assert not isinstance(raised.value, MailCursorExpired)

    async def test_app_only_runs_its_delta_inside_the_named_mailbox(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(self.delta_path(USERS)).respond_with_json(
            {"value": [], "@odata.deltaLink": httpserver.url_for("/v1.0/x")}
        )
        source = source_for(httpserver, app_only())

        assert await source.watermark() is not None
        await source.aclose()

    async def test_the_descriptor_and_the_watermark_agree(
        self, httpserver: Any
    ) -> None:
        """A promise of a delta that answers `None` is a mailbox nothing syncs."""
        httpserver.expect_request(self.delta_path()).respond_with_json(
            {"value": [], "@odata.deltaLink": httpserver.url_for("/v1.0/x")}
        )
        source = source_for(httpserver)

        mark = await source.watermark()
        await source.aclose()

        assert M365_DESCRIPTOR.supports_incremental is (mark is not None)


class TestFetchRaw:
    def test_it_is_a_coroutine_returning_a_stream_not_a_generator(self) -> None:
        # The engine writes `async for raw in await source.fetch_raw(refs)`;
        # an async generator makes that `await` fail at the call site.
        assert inspect.iscoroutinefunction(M365Source.fetch_raw)
        assert not inspect.isasyncgenfunction(M365Source.fetch_raw)

    async def test_the_bytes_arrive_in_the_order_they_were_asked_for(
        self, httpserver: Any
    ) -> None:
        serve_mail(httpserver)
        source = source_for(httpserver)
        refs = [MessageRef(provider_message_id=one) for one in MAIL]

        collected = [raw async for raw in await source.fetch_raw(refs)]
        await source.aclose()

        assert [one.ref.provider_message_id for one in collected] == list(MAIL)
        assert [one.raw for one in collected] == list(MAIL.values())

    async def test_the_listings_labels_travel_with_the_bytes(
        self, httpserver: Any
    ) -> None:
        # `$value` answers with MIME and no metadata, so the reference the
        # listing produced is the only one there is.
        serve_mail(httpserver)
        source = source_for(httpserver)
        ref = MessageRef(provider_message_id="AAMkA1", labels=("INBOX=", "Rot"))

        collected = [raw async for raw in await source.fetch_raw([ref])]
        await source.aclose()

        assert collected[0].ref.labels == ("INBOX=", "Rot")

    async def test_a_message_that_is_gone_is_one_skipped_message(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(f"{ME}/messages/AAMkA9/$value").respond_with_response(
            refusal("ErrorItemNotFound", 404)
        )
        source = source_for(httpserver)
        refs = [MessageRef(provider_message_id="AAMkA9")]

        with pytest.raises(MailPermanentError):
            [raw async for raw in await source.fetch_raw(refs)]
        await source.aclose()

    async def test_app_only_fetches_from_the_named_mailbox(
        self, httpserver: Any
    ) -> None:
        serve_mail(httpserver, USERS)
        source = source_for(httpserver, app_only())
        refs = [MessageRef(provider_message_id="AAMkA1")]

        collected = [raw async for raw in await source.fetch_raw(refs)]
        await source.aclose()

        assert collected[0].raw == MAIL["AAMkA1"]


class TestTheFactory:
    def test_it_is_the_port(self, httpserver: Any) -> None:
        source: MailSourcePort = source_for(httpserver)
        assert source.provider is MailProvider.M365

    def test_using_binds_one_configuration_and_returns_a_factory(
        self, httpserver: Any
    ) -> None:
        factory: MailSourceFactory = M365Source.using(config_for(httpserver))
        built = factory(None, delegated().to_secret())
        assert isinstance(built, M365Source)

    def test_a_secret_that_is_not_a_credential_fails_as_one(
        self, httpserver: Any
    ) -> None:
        factory = M365Source.using(config_for(httpserver))
        with pytest.raises(MailAuthError):
            factory(None, "{}")

    def test_the_descriptor_hangs_off_the_class_for_the_composition_root(
        self,
    ) -> None:
        assert M365Source.DESCRIPTOR is M365_DESCRIPTOR

    async def test_create_reads_its_configuration_from_the_environment(self) -> None:
        """What a composition root that built no config of its own registers.

        Nothing is sent: building a source opens an `httpx` pool and nothing
        else, so this stays as offline as every other test in the file.
        """
        built = M365Source.create(None, delegated().to_secret())

        assert isinstance(built, M365Source)
        await built.aclose()

    async def test_closing_twice_is_safe(self, httpserver: Any) -> None:
        source = source_for(httpserver)
        await source.aclose()
        await source.aclose()


class TestRotation:
    async def test_a_token_entra_rotated_mid_run_reaches_to_secret(
        self, httpserver: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`app/worker.py` reads exactly this and writes it back."""

        async def fake(current: Any, **_kwargs: Any) -> Any:
            return current.model_copy(
                update={
                    "access_token": MINTED,
                    "refresh_token": ROTATED,
                    "expires_at": datetime.now(UTC) + timedelta(hours=1),
                }
            )

        monkeypatch.setattr(client_module, "refresh_async", fake)
        httpserver.expect_request(ME).respond_with_handler(
            Script(refusal("InvalidAuthenticationToken", 401), json_response(IDENTITY))
        )
        opened_with = delegated().to_secret()
        source = source_for(httpserver)

        await source.verify()
        await source.aclose()

        assert source.credentials.to_secret() != opened_with
        assert json.loads(source.credentials.to_secret())["refresh_token"] == ROTATED
