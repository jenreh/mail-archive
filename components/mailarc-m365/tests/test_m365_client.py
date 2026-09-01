"""One question is decided here: when Graph says no, what does the engine do?

A rate limit read as an auth failure burns a working account; a revoked token
read as transient has the engine retry forever instead of asking for consent.
Both are one status code apart, so every branch of the mapping has a test — and
so does the promise that no `httpx` exception ever escapes, because an engine
that catches `MailTransientError` will not catch a `ConnectError`.

Two of these are Graph's own: a **410** means different things depending on
whether a delta was asked for, and a cursor is a whole **URL**, which is the
one thing no other provider's client has to police.

**No test here talks to Microsoft.** Every call goes to a local
`pytest-httpserver`, and MSAL is replaced at `refresh_async` — which is also
the whole reason `M365Config` carries its URLs as settings.
"""

import json
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from werkzeug import Request, Response

from mailarc_core.mail.errors import (
    MailAuthError,
    MailCursorExpired,
    MailPermanentError,
    MailTransientError,
)
from mailarc_m365.source import client as module
from mailarc_m365.source.client import GraphApiError, GraphClient
from mailarc_m365.source.config import M365Config
from mailarc_m365.source.credentials import M365DelegatedCredentials

API_ROOT = "/v1.0"
ME = f"{API_ROOT}/me"
LIVE = "eyJ0.live"  # noqa: S105 - a fixture
MINTED = "eyJ0.minted"  # noqa: S105 - a fixture
STORED = "0.AR-stored"  # noqa: S105 - a fixture
ROTATED = "0.AR-rotated"  # noqa: S105 - a fixture


def config_for(httpserver: Any) -> M365Config:
    """A configuration that points the whole adapter at the local server."""
    return M365Config(
        client_id="a-client",
        api_base_url=httpserver.url_for(API_ROOT),
        authority_host="http://127.0.0.1:1",  # never reached; MSAL is replaced
        request_timeout=5.0,
    )


def credentials(
    *, access_token: str | None = LIVE, minutes: int = 60
) -> M365DelegatedCredentials:
    return M365DelegatedCredentials(
        tenant_id="contoso.onmicrosoft.com",
        refresh_token=STORED,
        access_token=access_token,
        expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
    )


def mint(monkeypatch: pytest.MonkeyPatch, *, rotated: bool = False) -> list[Any]:
    """Replace the MSAL round trip with one that hands back a live token."""
    calls: list[Any] = []

    async def fake(current: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        update: dict[str, Any] = {
            "access_token": MINTED,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
        if rotated:
            update["refresh_token"] = ROTATED
        return current.model_copy(update=update)

    monkeypatch.setattr(module, "refresh_async", fake)
    return calls


def json_response(body: object, status: int = 200, **headers: str) -> Response:
    return Response(
        json.dumps(body),
        status=status,
        content_type="application/json",
        headers=headers,
    )


def refusal(code: str, message: str = "denied") -> dict[str, object]:
    """Graph's error envelope, the shape the API really sends."""
    return {"error": {"code": code, "message": message}}


class Script:
    """Answers a fixed list of replies in order, repeating the last one.

    `pytest-httpserver` handlers are permanent, so this is how a call that has
    to fail once and then succeed gets written down.
    """

    def __init__(self, *replies: Response) -> None:
        self._replies = replies
        self.seen: list[str | None] = []

    def __call__(self, request: Request) -> Response:
        reply = self._replies[min(len(self.seen), len(self._replies) - 1)]
        self.seen.append(request.headers.get("Authorization"))
        return reply


class TestTheHappyPath:
    async def test_a_get_returns_the_decoded_object(self, httpserver: Any) -> None:
        httpserver.expect_request(ME).respond_with_json({"id": "8f4c"})
        client = GraphClient(credentials(), config_for(httpserver))

        assert await client.get_json("/me") == {"id": "8f4c"}
        await client.aclose()

    async def test_every_request_carries_the_access_token(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(ME).respond_with_json({"id": "8f4c"})
        client = GraphClient(credentials(), config_for(httpserver))

        await client.get_json("/me")
        await client.aclose()

        assert httpserver.log[0][0].headers["Authorization"] == f"Bearer {LIVE}"

    async def test_a_stale_token_is_replaced_before_the_call_goes_out(
        self, httpserver: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pre-flight refresh is what keeps the 401 path rare.
        mint(monkeypatch)
        httpserver.expect_request(ME).respond_with_json({"id": "8f4c"})
        client = GraphClient(credentials(access_token=None), config_for(httpserver))

        await client.get_json("/me")
        await client.aclose()

        assert httpserver.log[0][0].headers["Authorization"] == f"Bearer {MINTED}"

    async def test_extra_headers_ride_along_with_the_authorization(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(ME).respond_with_json({})
        client = GraphClient(credentials(), config_for(httpserver))

        await client.get_json("/me", headers={"Prefer": "odata.maxpagesize=500"})
        await client.aclose()

        assert httpserver.log[0][0].headers["Prefer"] == "odata.maxpagesize=500"

    async def test_closing_twice_is_safe(self, httpserver: Any) -> None:
        client = GraphClient(credentials(), config_for(httpserver))
        await client.aclose()
        await client.aclose()


class TestTheExpiredToken:
    async def test_a_401_is_refreshed_once_and_the_call_repeated(
        self, httpserver: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mint(monkeypatch)
        script = Script(
            json_response(refusal("InvalidAuthenticationToken"), 401),
            json_response({"id": "8f4c"}),
        )
        httpserver.expect_request(ME).respond_with_handler(script)
        client = GraphClient(credentials(), config_for(httpserver))

        assert await client.get_json("/me") == {"id": "8f4c"}
        await client.aclose()

        assert script.seen == [f"Bearer {LIVE}", f"Bearer {MINTED}"]

    async def test_a_second_401_is_a_credential_and_not_a_clock(
        self, httpserver: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mint(monkeypatch)
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("InvalidAuthenticationToken"), 401)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailAuthError):
            await client.get_json("/me")
        await client.aclose()

    async def test_a_rotated_refresh_token_is_the_one_the_owner_reads(
        self, httpserver: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mint(monkeypatch, rotated=True)
        httpserver.expect_request(ME).respond_with_handler(
            Script(json_response(refusal("x"), 401), json_response({"id": "8f4c"}))
        )
        client = GraphClient(credentials(), config_for(httpserver))

        await client.get_json("/me")
        await client.aclose()

        rotated = client.credentials
        assert isinstance(rotated, M365DelegatedCredentials)
        assert rotated.refresh_token == ROTATED
        assert json.loads(rotated.to_secret())["refresh_token"] == ROTATED


class TestTheTaxonomy:
    async def test_a_429_carries_graphs_own_retry_after(self, httpserver: Any) -> None:
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("TooManyRequests"), 429, **{"Retry-After": "30"})
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailTransientError) as raised:
            await client.get_json("/me")
        await client.aclose()

        assert raised.value.retry_after == 30.0

    async def test_a_429_without_the_header_is_still_transient(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("TooManyRequests"), 429)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailTransientError) as raised:
            await client.get_json("/me")
        await client.aclose()

        assert raised.value.retry_after is None

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_every_5xx_is_transient(self, httpserver: Any, status: int) -> None:
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("ServiceUnavailable"), status)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailTransientError):
            await client.get_json("/me")
        await client.aclose()

    async def test_a_403_is_a_permission_this_grant_does_not_carry(
        self, httpserver: Any
    ) -> None:
        # Unlike Gmail, Graph does not spend its quota as a 403.
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("ErrorAccessDenied"), 403)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailAuthError, match="ErrorAccessDenied"):
            await client.get_json("/me")
        await client.aclose()

    async def test_a_404_skips_one_message_by_default(self, httpserver: Any) -> None:
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("ErrorItemNotFound"), 404)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailPermanentError):
            await client.get_json("/me")
        await client.aclose()

    async def test_a_404_means_what_the_caller_asked_it_to(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("ErrorItemNotFound"), 404)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailCursorExpired):
            await client.get_json("/me", not_found=MailCursorExpired)
        await client.aclose()

    async def test_a_410_is_permanent_unless_the_caller_says_otherwise(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("resyncRequired"), 410)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailPermanentError) as raised:
            await client.get_json("/me")
        await client.aclose()

        assert not isinstance(raised.value, MailCursorExpired)

    async def test_a_410_the_delta_asked_for_is_a_full_resync(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("resyncRequired"), 410)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailCursorExpired, match="resyncRequired"):
            await client.get_json("/me", gone=MailCursorExpired)
        await client.aclose()

    async def test_anything_else_costs_this_call_and_not_the_account(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(ME).respond_with_response(
            json_response(refusal("BadRequest"), 400)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailPermanentError):
            await client.get_json("/me")
        await client.aclose()

    async def test_a_refusal_with_no_json_at_all_is_still_a_refusal(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(ME).respond_with_data(
            "<html>gateway</html>", status=502, content_type="text/html"
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailTransientError):
            await client.get_json("/me")
        await client.aclose()


class TestNoHttpxEscapes:
    async def test_an_unreachable_host_is_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = M365Config(
            client_id="a-client",
            api_base_url=f"http://127.0.0.1:{port}/v1.0",
            request_timeout=2.0,
        )
        client = GraphClient(credentials(), config)

        with pytest.raises(MailTransientError, match="unreachable"):
            await client.get_json("/me")
        await client.aclose()

    async def test_a_200_that_is_not_json_is_transient(self, httpserver: Any) -> None:
        httpserver.expect_request(ME).respond_with_data(
            "not json", content_type="text/plain"
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailTransientError, match="not JSON"):
            await client.get_json("/me")
        await client.aclose()

    async def test_a_200_that_is_a_list_is_transient(self, httpserver: Any) -> None:
        httpserver.expect_request(ME).respond_with_json([1, 2, 3])
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailTransientError, match="list"):
            await client.get_json("/me")
        await client.aclose()

    async def test_a_message_id_the_url_cannot_carry_is_one_skipped_message(
        self, httpserver: Any
    ) -> None:
        """The hole `except httpx.RequestError` leaves open.

        `json.loads` produces lone surrogates from a `\\udcff` escape, so a
        provider message id really can be a string no URL can carry — and
        encoding it raises `UnicodeEncodeError` from inside httpx, which is
        neither a `RequestError` nor an `httpx` exception at all. It escaped
        the adapter entirely before this test existed.
        """
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailPermanentError, match="cannot be asked"):
            await client.get_bytes("/me/messages/\udcff/$value")
        await client.aclose()

        assert httpserver.log == []

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (httpx.InvalidURL("no host"), MailPermanentError),
            (httpx.StreamError("consumed"), MailPermanentError),
            (httpx.ConnectError("refused"), MailTransientError),
            (httpx.RemoteProtocolError("truncated"), MailTransientError),
            (httpx.ReadTimeout("too slow"), MailTransientError),
            # Whatever the library grows next, as long as it is about the
            # exchange: the engine is told to try the exchange again.
            (httpx.HTTPError("something new"), MailTransientError),
        ],
    )
    async def test_no_httpx_exception_reaches_the_engine_by_any_route(
        self,
        httpserver: Any,
        monkeypatch: pytest.MonkeyPatch,
        raised: Exception,
        expected: type[Exception],
    ) -> None:
        """`InvalidURL` is an `Exception` and `StreamError` a `RuntimeError`.

        Neither descends from `RequestError`, so catching that alone lets them
        past an engine which only knows the four errors.
        """
        client = GraphClient(credentials(), config_for(httpserver))

        async def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise raised

        monkeypatch.setattr(client._http, "get", explode)

        with pytest.raises(expected):
            await client.get_json("/me")
        await client.aclose()


class TestTheRawBytes:
    async def test_the_bytes_come_back_exactly_as_sent(self, httpserver: Any) -> None:
        raw = b"From: a@b.test\r\nSubject: eins\r\n\r\nText.\r\n"
        httpserver.expect_request(f"{API_ROOT}/me/messages/A/$value").respond_with_data(
            raw, content_type="text/plain"
        )
        client = GraphClient(credentials(), config_for(httpserver))

        assert await client.get_bytes("/me/messages/A/$value") == raw
        await client.aclose()

    async def test_an_empty_body_is_a_skipped_message(self, httpserver: Any) -> None:
        httpserver.expect_request(f"{API_ROOT}/me/messages/A/$value").respond_with_data(
            b"", content_type="text/plain"
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailPermanentError, match="empty body"):
            await client.get_bytes("/me/messages/A/$value")
        await client.aclose()

    async def test_an_expired_token_is_refreshed_once_here_too(
        self, httpserver: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mint(monkeypatch)
        raw = b"From: a@b.test\r\n\r\nhi\r\n"
        httpserver.expect_request(
            f"{API_ROOT}/me/messages/A/$value"
        ).respond_with_handler(
            Script(
                json_response(refusal("InvalidAuthenticationToken"), 401),
                Response(raw, content_type="text/plain"),
            )
        )
        client = GraphClient(credentials(), config_for(httpserver))

        assert await client.get_bytes("/me/messages/A/$value") == raw
        await client.aclose()

    async def test_a_404_on_a_message_is_one_skipped_message(
        self, httpserver: Any
    ) -> None:
        httpserver.expect_request(
            f"{API_ROOT}/me/messages/A/$value"
        ).respond_with_response(json_response(refusal("ErrorItemNotFound"), 404))
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailPermanentError):
            await client.get_bytes("/me/messages/A/$value")
        await client.aclose()


class TestTheUrl:
    async def test_a_link_graph_handed_out_is_followed(self, httpserver: Any) -> None:
        httpserver.expect_request(f"{API_ROOT}/me/messages").respond_with_json({})
        client = GraphClient(credentials(), config_for(httpserver))

        assert (
            await client.get_json(httpserver.url_for(f"{API_ROOT}/me/messages")) == {}
        )
        await client.aclose()

    async def test_a_link_pointing_elsewhere_never_gets_the_token(
        self, httpserver: Any
    ) -> None:
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailPermanentError, match="refusing to send"):
            await client.get_json("https://evil.example.test/v1.0/me/messages")
        await client.aclose()

        assert httpserver.log == []


class TestTheErrorEnvelope:
    def test_it_reads_the_code_and_the_message(self) -> None:
        described = GraphApiError.model_validate(
            {"code": "resyncRequired", "message": "Resync is required."}
        )
        assert described.describe() == "resyncRequired Resync is required."

    def test_an_envelope_that_is_not_there_describes_nothing(self) -> None:
        assert GraphApiError().describe() == ""

    async def test_a_body_whose_error_is_not_an_object_describes_nothing(
        self, httpserver: Any
    ) -> None:
        # A proxy or a captive portal is still a refusal, and the status code
        # already said what to do about it.
        httpserver.expect_request(ME).respond_with_response(
            json_response({"error": "gateway"}, 404)
        )
        client = GraphClient(credentials(), config_for(httpserver))

        with pytest.raises(MailPermanentError) as raised:
            await client.get_json("/me")
        await client.aclose()

        assert "gateway" not in str(raised.value)
