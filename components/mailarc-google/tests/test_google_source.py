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
from collections.abc import Sequence
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
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.credentials import GmailCredentials
from mailarc_google.source.model import GMAIL_DESCRIPTOR
from mailarc_google.source.source import (
    GMAIL_MAX_PAGE_SIZE,
    MESSAGE_ADDED,
    GmailSource,
)

API_ROOT = "/gmail/v1"
TOKEN_PATH = "/token"  # noqa: S105 - a URL path
PROFILE = f"{API_ROOT}/users/me/profile"
LABELS = f"{API_ROOT}/users/me/labels"
MESSAGES = f"{API_ROOT}/users/me/messages"
HISTORY = f"{API_ROOT}/users/me/history"
ONE_MESSAGE = re.compile(rf"^{re.escape(MESSAGES)}/")

CLIENT_ID = "123456789-example.apps.googleusercontent.com"
CLIENT_SECRET = "the-users-own-client-secret"  # noqa: S105 - a fixture
REFRESH_TOKEN = "1//stored-refresh-token"  # noqa: S105 - a fixture
LIVE = "ya29.live"
MINTED = "ya29.minted"

ADDRESS = "jens@example.com"
WATERMARK = "884411"
"""The `historyId` `getProfile` hands out — where a delta starts."""

PROFILE_BODY = {"emailAddress": ADDRESS, "messagesTotal": 3, "historyId": WATERMARK}
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

DELIVERED = {
    "18c4": b"From: dora@example.com\r\nSubject: vier\r\n\r\nText.\r\n",
    "18c5": b"From: emil@example.com\r\nSubject: fuenf\r\n\r\nText.\r\n",
}
"""What arrives *after* the full import — never listed, only ever in history."""

EVERYTHING = MAILBOX | DELIVERED
"""Fetchable by id. `messages.list` still only knows `MAILBOX`, which is the
point: a delta that fell back to listing would come up empty here."""


def credentials(httpserver, *, access_token: str | None = LIVE) -> GmailCredentials:
    """A stored credential whose token endpoint is the local server."""
    return GmailCredentials(
        refresh_token=REFRESH_TOKEN,
        token_uri=httpserver.url_for(TOKEN_PATH),
        access_token=access_token,
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )


def config_for(httpserver, **overrides: Any) -> GmailConfig:
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
        if identifier not in EVERYTHING:
            return json_response(
                {"error": {"code": 404, "message": "Requested entity was not found."}},
                status=404,
            )
        return json_response(
            {
                "id": identifier,
                "threadId": f"t-{identifier}",
                "labelIds": ["INBOX", "Label_12"],
                "sizeEstimate": len(EVERYTHING[identifier]),
                "raw": base64.urlsafe_b64encode(EVERYTHING[identifier])
                .decode()
                .rstrip("="),
            }
        )

    httpserver.expect_request(PROFILE).respond_with_json(PROFILE_BODY)
    httpserver.expect_request(LABELS).respond_with_json(LABEL_BODY)
    httpserver.expect_request(ONE_MESSAGE).respond_with_handler(message)
    httpserver.expect_request(MESSAGES).respond_with_handler(listing)


def serve_history(
    httpserver, *, arrived: Sequence[str] = tuple(DELIVERED), page_size: int = 2
) -> None:
    """`users.history.list`, with Gmail's own rule about a start that aged out.

    404 for any `startHistoryId` but the one `getProfile` handed out — which is
    what Gmail does once a history id is older than roughly a week, and the
    refusal the whole fallback hangs on. It answers that way on the *second*
    page too, so a cursor that lost the start id on its way through the engine
    fails a test here instead of quietly re-walking a mailbox in production.
    """

    def history(request: Request) -> Response:
        if request.args.get("startHistoryId") != WATERMARK:
            return json_response(
                {"error": {"code": 404, "message": "Requested entity was not found."}},
                status=404,
            )
        start = int(request.args.get("pageToken", 0))
        page = arrived[start : start + page_size]
        following = start + len(page)
        body: dict[str, object] = {
            "history": [
                {
                    "id": str(884412 + index),
                    "messagesAdded": [{"message": {"id": one, "threadId": f"t-{one}"}}],
                }
                for index, one in enumerate(page, start=start)
            ],
            "historyId": "884499",
        }
        if following < len(arrived):
            body["nextPageToken"] = str(following)
        return json_response(body)

    httpserver.expect_request(HISTORY).respond_with_handler(history)


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

    async def test_gmails_own_ceiling_binds_even_when_the_configuration_does_not(
        self, httpserver
    ) -> None:
        """`GmailConfig.page_size` carries no upper bound, on purpose.

        A number above the ceiling is a misconfiguration that costs nothing —
        the adapter clamps it — where a validator would refuse to start the
        whole application over a tuning knob. Without the clamp Gmail answers
        400, which `_refusal` maps to a permanent error, so *every* listing of
        every run would fail while the account looked healthy.
        """
        serve_mailbox(httpserver)
        source = GmailSource(
            credentials(httpserver), config_for(httpserver, page_size=9000)
        )

        await source.list_messages(None, limit=9999)

        assert queries(httpserver, MESSAGES)[0]["maxResults"] == str(
            GMAIL_MAX_PAGE_SIZE
        )
        assert GMAIL_MAX_PAGE_SIZE == 500, "Gmail's documented maximum"

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
            {
                "access_token": MINTED,
                "expires_in": 3599,
            }
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


class TestTheDelta:
    """Phase 7: the same port method, with the cursor the last run left behind.

    Everything below leans on one fact of the fixture — `messages.list` knows
    only `MAILBOX`, while `DELIVERED` is fetchable by id and appears in nothing
    but history. A delta that quietly fell back to listing would therefore come
    back empty, rather than come back right for the wrong reason.
    """

    async def test_the_watermark_is_the_profiles_history_id(self, source) -> None:
        watermark = await source.watermark()

        assert watermark is not None
        assert watermark.token == WATERMARK
        assert watermark.kind is SyncCursorKind.INCREMENTAL

    async def test_the_descriptor_and_the_watermark_agree(self, source) -> None:
        """The pairing the sixth method exists for.

        A descriptor promising a delta while `watermark()` answers `None` is a
        mailbox the scheduler queues forever and nothing ever fetches, and no
        other part of the system is placed to notice.
        """
        watermark = await source.watermark()

        assert GMAIL_DESCRIPTOR.supports_incremental is (watermark is not None)

    async def test_a_second_run_fetches_exactly_the_message_that_arrived(
        self, httpserver, source
    ) -> None:
        """The phase 7 DoD, as far as one adapter can prove it.

        The watermark is read *before* the import, which is what the engine
        does and why the delta is complete: a mark taken afterwards would miss
        whatever landed while the walk was running.
        """
        serve_history(httpserver, arrived=("18c4",))
        before = await source.watermark()

        assert await drain(source, limit=2) == list(MAILBOX)
        assert before is not None
        page = await source.list_messages(before, limit=100)
        fetched = [
            raw.ref.provider_message_id
            async for raw in await source.fetch_raw(page.refs)
        ]

        assert fetched == ["18c4"]
        assert page.next_cursor is None
        assert page.estimated_total == 1, "history sends no estimate of its own"

    async def test_it_asks_only_for_what_was_added(self, httpserver, source) -> None:
        """An archive never deletes, so the other three history types would
        only ever produce records to discard — and bigger pages to discard them
        from."""
        serve_history(httpserver)

        await source.list_messages(await source.watermark(), limit=7)

        assert queries(httpserver, HISTORY) == [
            {
                "startHistoryId": WATERMARK,
                "historyTypes": MESSAGE_ADDED,
                "maxResults": "7",
            }
        ]

    async def test_every_page_resends_the_start_and_the_walk_then_ends(
        self, httpserver, source
    ) -> None:
        """Two values, one `SyncCursor.token` — and a walk that terminates.

        `startHistoryId` is required on every call and `pageToken` on every one
        after the first, so both ride sealed in the token. The last page hands
        back `None` rather than the reply's own `historyId`: a cursor that is
        never `None` is an engine loop that never breaks.
        """
        serve_history(httpserver, page_size=1)

        first = await source.list_messages(await source.watermark(), limit=100)
        assert first.next_cursor is not None
        second = await source.list_messages(first.next_cursor, limit=100)

        assert [one.provider_message_id for one in first.refs] == ["18c4"]
        assert [one.provider_message_id for one in second.refs] == ["18c5"]
        assert second.next_cursor is None
        assert [one["startHistoryId"] for one in queries(httpserver, HISTORY)] == [
            WATERMARK,
            WATERMARK,
        ]

    async def test_one_message_in_two_records_is_fetched_once(
        self, httpserver, source
    ) -> None:
        """Gmail names an id again for every change it took part in."""
        serve_history(httpserver, arrived=("18c4", "18c4"))

        page = await source.list_messages(await source.watermark(), limit=100)

        assert [one.provider_message_id for one in page.refs] == ["18c4"]

    async def test_a_full_cursor_still_walks_the_mailbox(
        self, httpserver, source
    ) -> None:
        """The kind picks the endpoint, so a resumed import cannot become a delta."""
        serve_history(httpserver)

        page = await source.list_messages(None, limit=2)
        assert page.next_cursor is not None
        await source.list_messages(page.next_cursor, limit=2)

        assert queries(httpserver, HISTORY) == []


class TestWhatA404MeansToTheEngine:
    """One status code, two answers, and only the caller knows which it asked.

    A `startHistoryId` Gmail no longer keeps and a message deleted between the
    listing and the fetch are both 404, and the engine does opposite things
    with them: throw the cursor away and walk the whole mailbox, or skip this
    one message and write a row. Same server, same account, two calls — which
    is the proof that the mapping was not simply loosened for everyone.
    """

    async def test_the_two_404s_land_on_different_answers(
        self, httpserver, source
    ) -> None:
        serve_history(httpserver)
        aged_out = SyncCursor(
            provider=MailProvider.GMAIL,
            token="1",
            kind=SyncCursorKind.INCREMENTAL,
        )
        gone = [MessageRef(provider_message_id="deleted-since-listing")]

        with pytest.raises(MailCursorExpired, match="404"):
            await source.list_messages(aged_out, limit=100)
        with pytest.raises(MailPermanentError, match="404"):
            [raw async for raw in await source.fetch_raw(gone)]

    def test_the_engines_per_message_handler_cannot_claim_the_cursor_one(
        self,
    ) -> None:
        """Which is why `MailCursorExpired` is a sibling and not a subclass."""
        assert not issubclass(MailCursorExpired, MailPermanentError)
