"""One question is decided here: when Gmail says no, what does the engine do?

A rate limit read as an auth failure burns a working account; a revoked token
read as transient has the engine retry forever instead of asking for consent.
Both are one status code apart, so every branch of the mapping has a test — and
so does the promise that no `httpx` exception ever escapes, because an engine
that catches `MailTransientError` will not catch a `ConnectError`.

**No test here talks to Google.** Every call goes to a local
`pytest-httpserver`, which is also the whole reason `GmailConfig` carries the
two URLs as settings.
"""

import json
import socket
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from werkzeug import Request, Response

from mailarc_core.mail.errors import (
    MailAuthError,
    MailPermanentError,
    MailTransientError,
)
from mailarc_google.source.client import GmailApiError, GmailClient
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.credentials import GmailCredentials

API_ROOT = "/gmail/v1"
TOKEN_PATH = "/token"  # noqa: S105 - a URL path
PROFILE = f"{API_ROOT}/users/me/profile"

CLIENT_ID = "123456789-example.apps.googleusercontent.com"
CLIENT_SECRET = "the-users-own-client-secret"  # noqa: S105 - a fixture
REFRESH_TOKEN = "1//stored-refresh-token"  # noqa: S105 - a fixture
ROTATED_TOKEN = "1//rotated-refresh-token"  # noqa: S105 - a fixture
LIVE = "ya29.live"
MINTED = "ya29.minted"

ADDRESS = "jens@example.com"
PROFILE_BODY = {"emailAddress": ADDRESS, "messagesTotal": 12, "historyId": "9"}


def config_for(httpserver) -> GmailConfig:
    """A configuration that points the whole adapter at the local server."""
    return GmailConfig(
        api_base_url=httpserver.url_for(API_ROOT),
        token_uri=httpserver.url_for(TOKEN_PATH),
        request_timeout=5.0,
    )


def credentials(
    httpserver, *, access_token: str | None = LIVE, minutes: int = 60
) -> GmailCredentials:
    """A stored credential, live by default and stale when asked for.

    Its `token_uri` is the local server and never Google's: the refresh reads
    the *credential's* endpoint, not the config's, so a fixture that forgot
    this one field is a fixture that phones home.
    """
    return GmailCredentials(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        refresh_token=REFRESH_TOKEN,
        token_uri=httpserver.url_for(TOKEN_PATH),
        access_token=access_token,
        expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
    )


def json_response(body: object, status: int = 200) -> Response:
    """A werkzeug reply, for the handlers that answer differently each call."""
    return Response(json.dumps(body), status=status, content_type="application/json")


def refusal(
    reason: str, message: str = "denied", status: int = 403
) -> dict[str, object]:
    """Google's error envelope, the shape the API really sends."""
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"reason": reason, "message": message}],
        }
    }


class Script:
    """Answers a fixed list of replies in order, repeating the last one.

    `pytest-httpserver` handlers are permanent, so this is how a call that has
    to fail once and then succeed gets written down.
    """

    def __init__(self, *replies: Response) -> None:
        self._replies = replies
        self.calls = 0

    def __call__(self, request: Request) -> Response:
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return reply


def serve_token(httpserver, **overrides: object) -> None:
    """Let the token endpoint mint one, so a refresh can succeed."""
    body = {"access_token": MINTED, "expires_in": 3599} | overrides
    httpserver.expect_request(TOKEN_PATH, method="POST").respond_with_json(body)


def bearers(httpserver) -> list[str | None]:
    """The `Authorization` header of every request the server saw."""
    return [request.headers.get("Authorization") for request, _ in httpserver.log]


def closed_port() -> int:
    """A port nothing is listening on, for the dropped-connection case."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def profile(client: GmailClient) -> dict[str, object]:
    """The one call every test in this module makes."""
    try:
        return await client.get("/users/me/profile")
    finally:
        await client.aclose()


class TestTheAccessToken:
    async def test_every_request_carries_the_bearer(self, httpserver) -> None:
        httpserver.expect_request(PROFILE).respond_with_json(PROFILE_BODY)

        body = await profile(
            GmailClient(credentials(httpserver), config_for(httpserver))
        )

        assert body == PROFILE_BODY
        assert bearers(httpserver) == [f"Bearer {LIVE}"]

    async def test_a_stale_token_is_refreshed_before_the_call_goes_out(
        self, httpserver
    ) -> None:
        """Cheaper than the 401 it would otherwise cost on every call."""
        serve_token(httpserver)
        httpserver.expect_request(PROFILE).respond_with_json(PROFILE_BODY)

        await profile(
            GmailClient(
                credentials(httpserver, access_token=None), config_for(httpserver)
            )
        )

        assert bearers(httpserver)[-1] == f"Bearer {MINTED}"

    async def test_a_token_about_to_expire_counts_as_stale(self, httpserver) -> None:
        serve_token(httpserver)
        httpserver.expect_request(PROFILE).respond_with_json(PROFILE_BODY)

        await profile(
            GmailClient(credentials(httpserver, minutes=0), config_for(httpserver))
        )

        assert bearers(httpserver)[-1] == f"Bearer {MINTED}"

    async def test_a_401_refreshes_once_and_repeats_the_call(self, httpserver) -> None:
        serve_token(httpserver)
        script = Script(json_response({}, status=401), json_response(PROFILE_BODY))
        httpserver.expect_request(PROFILE).respond_with_handler(script)

        body = await profile(
            GmailClient(credentials(httpserver), config_for(httpserver))
        )

        assert body == PROFILE_BODY
        assert script.calls == 2, "the retried call is the point"
        assert bearers(httpserver)[-1] == f"Bearer {MINTED}"

    async def test_a_second_401_is_the_credential_and_not_the_clock(
        self, httpserver
    ) -> None:
        """One refresh, never a retry loop — the user has to consent again."""
        serve_token(httpserver)
        script = Script(json_response(refusal("authError", status=401), status=401))
        httpserver.expect_request(PROFILE).respond_with_handler(script)

        with pytest.raises(MailAuthError, match="401"):
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

        assert script.calls == 2

    async def test_a_refresh_the_endpoint_rejects_is_terminal(self, httpserver) -> None:
        httpserver.expect_request(TOKEN_PATH, method="POST").respond_with_json(
            {"error": "invalid_grant", "error_description": "Token has been revoked."},
            status=400,
        )
        httpserver.expect_request(PROFILE).respond_with_json(PROFILE_BODY)

        with pytest.raises(MailAuthError, match="revoked"):
            await profile(
                GmailClient(
                    credentials(httpserver, access_token=None), config_for(httpserver)
                )
            )

    async def test_a_rotated_refresh_token_is_kept_where_it_can_be_stored(
        self, httpserver
    ) -> None:
        """Phase 3 item 3: the new one has to reach `mail_credentials`."""
        serve_token(httpserver, refresh_token=ROTATED_TOKEN)
        httpserver.expect_request(PROFILE).respond_with_json(PROFILE_BODY)
        client = GmailClient(
            credentials(httpserver, access_token=None), config_for(httpserver)
        )

        await profile(client)

        assert client.credentials.refresh_token == ROTATED_TOKEN


class TestTheStatusMapping:
    async def test_a_rate_limit_carries_googles_own_retry_after(
        self, httpserver
    ) -> None:
        httpserver.expect_request(PROFILE).respond_with_data(
            "slow down", status=429, headers={"Retry-After": "30"}
        )

        with pytest.raises(MailTransientError) as raised:
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

        assert raised.value.retry_after == 30.0

    async def test_a_retry_after_date_becomes_seconds(self, httpserver) -> None:
        """RFC 9110 allows both forms and Google sends both."""
        when = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
        httpserver.expect_request(PROFILE).respond_with_data(
            "slow down", status=429, headers={"Retry-After": when}
        )

        with pytest.raises(MailTransientError) as raised:
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

        assert raised.value.retry_after is not None
        assert 60.0 < raised.value.retry_after <= 90.0

    async def test_a_date_without_a_zone_is_read_as_utc(self, httpserver) -> None:
        """The alternative is a floor that is off by the reader's own offset."""
        naive = (datetime.now(UTC) + timedelta(seconds=90)).strftime(
            "%a, %d %b %Y %H:%M:%S"
        )
        httpserver.expect_request(PROFILE).respond_with_data(
            "slow down", status=429, headers={"Retry-After": naive}
        )

        with pytest.raises(MailTransientError) as raised:
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

        assert raised.value.retry_after is not None
        assert 60.0 < raised.value.retry_after <= 90.0

    async def test_an_unreadable_retry_after_is_simply_no_floor(
        self, httpserver
    ) -> None:
        httpserver.expect_request(PROFILE).respond_with_data(
            "slow down", status=429, headers={"Retry-After": "soon-ish"}
        )

        with pytest.raises(MailTransientError) as raised:
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

        assert raised.value.retry_after is None

    async def test_a_403_that_is_really_a_quota_is_worth_trying_again(
        self, httpserver
    ) -> None:
        """§11: Gmail spends its 250 units/user/s as a 403 as often as a 429."""
        httpserver.expect_request(PROFILE).respond_with_json(
            refusal("userRateLimitExceeded", "User-rate limit exceeded."), status=403
        )

        with pytest.raises(MailTransientError, match="rate limit"):
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

    async def test_a_403_that_is_a_real_refusal_is_terminal(self, httpserver) -> None:
        httpserver.expect_request(PROFILE).respond_with_json(
            refusal("insufficientPermissions", "Insufficient Permission."), status=403
        )

        with pytest.raises(MailAuthError, match="Insufficient Permission"):
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

    async def test_a_5xx_is_transient(self, httpserver) -> None:
        httpserver.expect_request(PROFILE).respond_with_data(
            "backend error", status=503
        )

        with pytest.raises(MailTransientError, match="503"):
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

    async def test_a_404_is_one_message_gone_and_not_an_outage(
        self, httpserver
    ) -> None:
        httpserver.expect_request(PROFILE).respond_with_json(
            refusal("notFound", "Requested entity was not found.", 404), status=404
        )

        with pytest.raises(MailPermanentError, match="not found"):
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

    async def test_every_other_4xx_is_permanent_too(self, httpserver) -> None:
        """Retrying a malformed request produces the same malformed request."""
        httpserver.expect_request(PROFILE).respond_with_data("bad request", status=400)

        with pytest.raises(MailPermanentError, match="400"):
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))


class TestNoHttpxErrorEscapes:
    async def test_a_dropped_connection_is_transient(self, httpserver) -> None:
        config = GmailConfig(
            api_base_url=f"http://127.0.0.1:{closed_port()}/gmail/v1",
            request_timeout=2.0,
        )

        with pytest.raises(MailTransientError, match="unreachable"):
            await profile(GmailClient(credentials(httpserver), config))

    async def test_a_hung_socket_becomes_a_retry_and_not_a_stuck_worker(
        self, httpserver
    ) -> None:
        def linger(request: Request) -> Response:
            time.sleep(0.4)
            return json_response(PROFILE_BODY)

        httpserver.expect_request(PROFILE).respond_with_handler(linger)
        config = config_for(httpserver).model_copy(update={"request_timeout": 0.05})

        with pytest.raises(MailTransientError, match="timed out"):
            await profile(GmailClient(credentials(httpserver), config))

    async def test_a_200_that_is_not_json_is_worth_trying_again(
        self, httpserver
    ) -> None:
        """A captive portal or a bad gateway day, never Gmail itself."""
        httpserver.expect_request(PROFILE).respond_with_data(
            "<html>a proxy</html>", status=200
        )

        with pytest.raises(MailTransientError, match="not JSON"):
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))

    async def test_a_json_body_that_is_not_an_object_is_transient(
        self, httpserver
    ) -> None:
        httpserver.expect_request(PROFILE).respond_with_json([1, 2, 3])

        with pytest.raises(MailTransientError, match="list"):
            await profile(GmailClient(credentials(httpserver), config_for(httpserver)))


class TestTheErrorEnvelope:
    def test_a_body_that_is_not_json_reads_as_nothing(self) -> None:
        """A captive portal is still a refusal; the status code already said so."""
        described = GmailApiError.read(httpx.Response(500, text="<html>no</html>"))

        assert described.describe() == ""
        assert described.rate_limited() is False

    def test_a_json_body_that_is_not_googles_envelope_reads_as_nothing(self) -> None:
        described = GmailApiError.read(httpx.Response(500, json={"error": "a string"}))

        assert described.describe() == ""

    def test_a_reason_google_does_not_send_is_not_a_rate_limit(self) -> None:
        assert GmailApiError(reasons=("forbidden",)).rate_limited() is False
        assert GmailApiError(reasons=("rateLimitExceeded",)).rate_limited() is True


class TestClosing:
    async def test_aclose_is_safe_to_call_twice(self, httpserver) -> None:
        """§7.1 says so, and a worker that closes in a `finally` relies on it."""
        httpserver.expect_request(PROFILE).respond_with_json(PROFILE_BODY)
        client = GmailClient(credentials(httpserver), config_for(httpserver))

        await client.get("/users/me/profile")
        await client.aclose()
        await client.aclose()

    async def test_the_url_survives_a_root_with_a_trailing_slash(
        self, httpserver
    ) -> None:
        httpserver.expect_request(PROFILE).respond_with_json(PROFILE_BODY)
        base = config_for(httpserver)
        config = base.model_copy(update={"api_base_url": f"{base.api_base_url}/"})

        assert (
            await profile(GmailClient(credentials(httpserver), config)) == PROFILE_BODY
        )
