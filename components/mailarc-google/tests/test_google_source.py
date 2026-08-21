"""Gmail walked through nothing but the port, against a local HTTP server.

The tests that matter are the ones `FakeMailSource` already passes, because
that is what makes `MailSourcePort` an abstraction rather than a description of
Gmail: paging that ends, a stream rather than a list, labels that arrive with
the message, and every failure expressed in the taxonomy. Whatever is Gmail's
alone — `pageToken`, `format=raw`, `resultSizeEstimate` — is asserted on the
requests this adapter actually sends.

**No test here talks to Google**, which is the phase 3 DoD. `GmailConfig`
carries the API root and the credential carries the token endpoint, and both
point at `pytest-httpserver`.
"""

import base64
import inspect
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from werkzeug import Request, Response

from mailarc_core.mail.errors import MailAuthError, MailPermanentError
from mailarc_core.mail.model import (
    LabelKind,
    MailProvider,
    MessageRef,
    SyncCursor,
)
from mailarc_core.mail.ports import MailSourceFactory, MailSourcePort
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.credentials import GmailCredentials
from mailarc_google.source.model import GMAIL_DESCRIPTOR
from mailarc_google.source.source import (
    GMAIL_MAX_PAGE_SIZE,
    GmailSource,
)

API_ROOT = "/gmail/v1"
TOKEN_PATH = "/token"  # noqa: S105 - a URL path
PROFILE = f"{API_ROOT}/users/me/profile"
LABELS = f"{API_ROOT}/users/me/labels"
MESSAGES = f"{API_ROOT}/users/me/messages"
ONE_MESSAGE = re.compile(rf"^{re.escape(MESSAGES)}/")

CLIENT_ID = "123456789-example.apps.googleusercontent.com"
CLIENT_SECRET = "the-users-own-client-secret"  # noqa: S105 - a fixture
REFRESH_TOKEN = "1//stored-refresh-token"  # noqa: S105 - a fixture
LIVE = "ya29.live"
MINTED = "ya29.minted"

ADDRESS = "jens@example.com"
PROFILE_BODY = {"emailAddress": ADDRESS, "messagesTotal": 3, "historyId": "884411"}
LABEL_BODY = {
    "labels": [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "Label_12", "name": "Rechnungen", "type": "user"},
    ]
}
PAGE_TOKEN = "07495424"  # noqa: S105 - a page token, not a credential

MAILBOX = {
    "18c1": b"From: anna@example.com\r\nSubject: eins\r\n\r\nText.\r\n",
    "18c2": b"From: bob@example.com\r\nSubject: zwei\r\n\r\nText.\r\n",
    "18c3": b"From: cleo@example.com\r\nSubject: drei\r\n\r\nText.\r\n",
}


def credentials(httpserver, *, access_token: str | None = LIVE) -> GmailCredentials:
    """A stored credential whose token endpoint is the local server."""
    return GmailCredentials(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        refresh_token=REFRESH_TOKEN,
        token_uri=httpserver.url_for(TOKEN_PATH),
        access_token=access_token,
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )


def config_for(httpserver, **overrides: object) -> GmailConfig:
    """A configuration pointing the adapter at the local server."""
    return GmailConfig(
        api_base_url=httpserver.url_for(API_ROOT),
        token_uri=httpserver.url_for(TOKEN_PATH),
        request_timeout=5.0,
        **overrides,
    )


def serve_mailbox(httpserver, *, page_size: int = 2) -> None:
    """The three endpoints one import walks: profile, labels, messages."""
    identifiers = list(MAILBOX)

    def listing(request: Request) -> Response:
        """One page of ids, paged with Gmail's own `pageToken`."""
        start = (
            identifiers.index(request.args["pageToken"])
            if "pageToken" in request.args
            else 0
        )
        page = identifiers[start : start + page_size]
        following = start + len(page)
        body: dict[str, object] = {
            "messages": [{"id": one, "threadId": f"t-{one}"} for one in page],
            "resultSizeEstimate": len(identifiers),
        }
        if following < len(identifiers):
            body["nextPageToken"] = identifiers[following]
        return json_response(body)

    def message(request: Request) -> Response:
        """One message, `format=raw` and base64url as Gmail sends it."""
        identifier = request.path.rsplit("/", 1)[-1]
        if identifier not in MAILBOX:
            return json_response(
                {"error": {"code": 404, "message": "Requested entity was not found."}},
                status=404,
            )
        return json_response(
            {
                "id": identifier,
                "threadId": f"t-{identifier}",
                "labelIds": ["INBOX", "Label_12"],
                "sizeEstimate": len(MAILBOX[identifier]),
                "raw": base64.urlsafe_b64encode(MAILBOX[identifier])
                .decode()
                .rstrip("="),
            }
        )

    httpserver.expect_request(PROFILE).respond_with_json(PROFILE_BODY)
    httpserver.expect_request(LABELS).respond_with_json(LABEL_BODY)
    httpserver.expect_request(ONE_MESSAGE).respond_with_handler(message)
    httpserver.expect_request(MESSAGES).respond_with_handler(listing)


def json_response(body: object, status: int = 200) -> Response:
    return Response(json.dumps(body), status=status, content_type="application/json")


def queries(httpserver, path: str) -> list[dict[str, str]]:
    """The query string of every request to a path, in order."""
    return [dict(request.args) for request, _ in httpserver.log if request.path == path]


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


@pytest.fixture
def source(httpserver) -> GmailSource:
    serve_mailbox(httpserver)
    return GmailSource(credentials(httpserver), config_for(httpserver))


class TestTheRegistration:
    def test_it_declares_itself_as_gmail(self) -> None:
        assert GmailSource.provider is MailProvider.GMAIL
        assert GmailSource.DESCRIPTOR is GMAIL_DESCRIPTOR

    def test_create_reads_the_credential_out_of_the_decrypted_secret(
        self, httpserver, monkeypatch
    ) -> None:
        monkeypatch.setenv("app_google_api_base_url", httpserver.url_for(API_ROOT))
        secret = credentials(httpserver).to_secret()

        built = GmailSource.create(object(), secret)

        assert isinstance(built, GmailSource)
        assert built.credentials.refresh_token == REFRESH_TOKEN
        assert built._config.api_base_url == httpserver.url_for(API_ROOT)

    def test_using_binds_the_configuration_the_composition_root_built(
        self, httpserver
    ) -> None:
        """`MailSourceFactory` has no room for a config, so it is closed over."""
        factory: MailSourceFactory = GmailSource.using(config_for(httpserver))

        built = factory(object(), credentials(httpserver).to_secret())

        assert isinstance(built, GmailSource)
        assert built._config.api_base_url == httpserver.url_for(API_ROOT)

    def test_a_secret_that_is_not_a_credential_fails_as_one(self, httpserver) -> None:
        with pytest.raises(MailAuthError):
            GmailSource.using(config_for(httpserver))(object(), "{}")


class TestThePort:
    async def test_verify_reports_whose_mailbox_this_is(self, source) -> None:
        identity = await source.verify()

        assert identity.provider is MailProvider.GMAIL
        assert identity.address.address == ADDRESS

    async def test_list_labels_brings_both_kinds_back(self, source) -> None:
        labels = await source.list_labels()

        assert [one.name for one in labels] == ["INBOX", "Rechnungen"]
        assert [one.kind for one in labels] == [LabelKind.SYSTEM, LabelKind.USER]

    async def test_the_whole_mailbox_comes_back_one_page_at_a_time(
        self, source
    ) -> None:
        assert await drain(source, limit=2) == list(MAILBOX)

    async def test_paging_carries_gmails_own_token_and_then_stops(
        self, httpserver, source
    ) -> None:
        first = await source.list_messages(None, limit=2)
        second = await source.list_messages(first.next_cursor, limit=2)

        assert first.next_cursor is not None
        assert first.next_cursor.token == "18c3"  # noqa: S105 - a page token
        assert second.next_cursor is None
        assert queries(httpserver, MESSAGES)[1]["pageToken"] == "18c3"

    async def test_the_estimate_comes_from_gmails_own_guess(self, source) -> None:
        page = await source.list_messages(None, limit=2)

        assert page.estimated_total == len(MAILBOX)

    async def test_a_page_never_exceeds_what_the_adapter_may_ask_for(
        self, httpserver
    ) -> None:
        """The smallest of the engine's limit, the config and Gmail's own 500."""
        serve_mailbox(httpserver)
        source = GmailSource(
            credentials(httpserver), config_for(httpserver, page_size=5)
        )

        await source.list_messages(None, limit=2)
        await source.list_messages(None, limit=9999)

        assert [one["maxResults"] for one in queries(httpserver, MESSAGES)] == [
            "2",
            "5",
        ]
        assert GMAIL_MAX_PAGE_SIZE == 500

    async def test_fetch_raw_is_a_coroutine_that_returns_a_stream(self, source) -> None:
        """§7.1's shape: an async generator would break `await source.fetch_raw`."""
        pending = source.fetch_raw([MessageRef(provider_message_id="18c1")])

        assert inspect.iscoroutine(pending)
        assert not inspect.isasyncgen(pending)
        assert [raw.ref.provider_message_id async for raw in await pending] == ["18c1"]

    async def test_the_bytes_arrive_decoded_and_the_labels_beside_them(
        self, httpserver, source
    ) -> None:
        refs = [MessageRef(provider_message_id=one) for one in MAILBOX]

        fetched = [raw async for raw in await source.fetch_raw(refs)]

        assert [raw.raw for raw in fetched] == list(MAILBOX.values())
        assert fetched[0].ref.labels == ("INBOX", "Label_12")
        assert fetched[0].ref.provider_thread_id == "t-18c1"
        assert all(
            one["format"] == "raw" for one in queries(httpserver, f"{MESSAGES}/18c1")
        ), "phase 3 item 2: always format=raw"

    async def test_a_message_that_is_gone_is_one_to_skip_and_write_down(
        self, source
    ) -> None:
        refs = [MessageRef(provider_message_id="deleted-since-listing")]

        with pytest.raises(MailPermanentError, match="404"):
            [raw async for raw in await source.fetch_raw(refs)]

    async def test_an_expired_token_is_refreshed_once_and_the_run_goes_on(
        self, httpserver
    ) -> None:
        serve_mailbox(httpserver)
        httpserver.expect_request(TOKEN_PATH, method="POST").respond_with_json(
            {"access_token": MINTED, "expires_in": 3599}
        )
        source = GmailSource(
            credentials(httpserver, access_token=None), config_for(httpserver)
        )

        assert await drain(source, limit=2) == list(MAILBOX)
        assert [
            request.headers["Authorization"]
            for request, _ in httpserver.log
            if request.path.startswith(MESSAGES)
        ] == [f"Bearer {MINTED}"] * 5, "two listings and three fetches, all bearing it"
        assert len(queries(httpserver, TOKEN_PATH)) == 1, "once, not once per call"

    async def test_aclose_is_safe_to_call_twice(self, source) -> None:
        await source.verify()

        await source.aclose()
        await source.aclose()
