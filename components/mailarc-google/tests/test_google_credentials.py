"""Two things live or die here: the round trip and the taxonomy.

`mail_credentials.secret` is a structureless column (§8.1), so nothing but this
model guards what goes into it — a field that does not survive
`to_secret`/`from_secret` is an account that silently cannot sync after a
restart.

And the refresh is where §7.6 is decided for Gmail. A revoked token that came
back as "transient" would have the engine retry forever instead of asking the
user to consent again; a rate limit that came back as "auth" would burn a
working account. Both are one status code apart, so both are tested. **No test
here talks to Google** — every call goes to a local `pytest-httpserver`.
"""

import json
import socket
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from mailarc_core.mail.errors import MailAuthError, MailTransientError
from mailarc_google.source import credentials as credentials_module
from mailarc_google.source.credentials import (
    GmailCredentials,
    refresh,
    refresh_async,
)
from mailarc_google.source.model import (
    GMAIL_READONLY_SCOPE,
    GOOGLE_TOKEN_URI,
    GoogleTokenResponse,
)

CLIENT_ID = "123456789-example.apps.googleusercontent.com"
CLIENT_SECRET = "the-users-own-client-secret"  # noqa: S105 - a fixture
REFRESH_TOKEN = "1//stored-refresh-token"  # noqa: S105 - a fixture
CACHED = "ya29.cached"
ISSUED = "ya29.issued"
MINTED = "ya29.new"
ROTATED = "1//rotated"
INTRUDER = "sneaked-in"
ANY_ACCESS = "t"


def stored(token_uri: str = GOOGLE_TOKEN_URI) -> GmailCredentials:
    """A credential row as it comes back out of the encrypted column.

    No client id and no client secret: those belong to the installation, not
    to this mailbox, and live in `GmailConfig`.
    """
    return GmailCredentials(refresh_token=REFRESH_TOKEN, token_uri=token_uri)


def refreshed(credentials: GmailCredentials, **kwargs) -> GmailCredentials:
    """`refresh` with the configured OAuth client the caller now has to pass."""
    return refresh(
        credentials, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, **kwargs
    )


def closed_port() -> int:
    """A port nothing is listening on, for the connection-refused case."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class TestTheStructurelessColumn:
    def test_the_round_trip_keeps_every_field(self) -> None:
        expires_at = datetime.now(UTC) + timedelta(hours=1)
        original = stored().model_copy(
            update={"access_token": CACHED, "expires_at": expires_at}
        )

        restored = GmailCredentials.from_secret(original.to_secret())

        assert restored == original
        assert restored.expires_at == expires_at

    def test_the_secret_is_json_a_new_provider_needs_no_migration_for(self) -> None:
        secret = stored().to_secret()

        assert secret.startswith("{"), "the column holds JSON"
        assert "refresh_token" in secret

    def test_a_row_that_does_not_parse_fails_as_a_credential(self) -> None:
        """Not as a ValidationError — nothing upstream knows what that means."""
        with pytest.raises(MailAuthError, match="unreadable"):
            GmailCredentials.from_secret("not json at all")

    def test_a_credential_cannot_be_edited_in_place(self) -> None:
        """Frozen, so a refresh hands back a new object the caller must store."""
        with pytest.raises(ValueError, match="frozen"):
            stored().access_token = INTRUDER  # ty: ignore[invalid-assignment]

    def test_an_older_row_with_a_naive_expiry_is_read_as_utc(self) -> None:
        """google-auth writes naive UTC; comparing that to `now` would raise."""
        restored = GmailCredentials.from_secret(
            stored()
            .model_copy(update={"expires_at": datetime(2030, 1, 1, 12, 0)})  # noqa: DTZ001
            .to_secret()
        )

        assert restored.expires_at == datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


class TestWhenARefreshIsDue:
    def test_a_credential_that_never_had_a_token_needs_one(self) -> None:
        assert stored().needs_refresh() is True

    def test_a_token_without_a_known_expiry_is_not_trusted(self) -> None:
        assert (
            stored().model_copy(update={"access_token": ANY_ACCESS}).needs_refresh()
            is True
        )

    def test_a_token_good_for_another_hour_is_kept(self) -> None:
        fresh = stored().model_copy(
            update={
                "access_token": ANY_ACCESS,
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
        )

        assert fresh.needs_refresh() is False

    def test_a_token_expiring_within_the_leeway_is_already_stale(self) -> None:
        """It would otherwise die halfway through the batch that is using it."""
        expiring = stored().model_copy(
            update={
                "access_token": ANY_ACCESS,
                "expires_at": datetime.now(UTC) + timedelta(seconds=30),
            }
        )

        assert expiring.needs_refresh() is True
        assert expiring.needs_refresh(leeway=0) is False


class TestTheAuthorizationHeader:
    def test_it_is_what_the_api_client_sends(self) -> None:
        with_token = stored().model_copy(update={"access_token": CACHED})

        assert with_token.authorization_header() == f"Bearer {CACHED}"

    def test_asking_before_a_refresh_says_so_instead_of_earning_a_401(self) -> None:
        with pytest.raises(MailAuthError, match="refresh before"):
            stored().authorization_header()


class TestCarryingWhatWasIssued:
    def test_a_response_without_a_refresh_token_keeps_the_stored_one(self) -> None:
        """The usual case. Overwriting it with nothing locks the account out."""
        carried = stored().with_token(
            GoogleTokenResponse(access_token=MINTED, expires_in=3599)
        )

        assert carried.refresh_token == REFRESH_TOKEN
        assert carried.access_token == MINTED

    def test_a_rotated_refresh_token_replaces_the_stored_one(self) -> None:
        carried = stored().with_token(
            GoogleTokenResponse(access_token=MINTED, refresh_token=ROTATED)
        )

        assert carried.refresh_token == ROTATED

    def test_the_expiry_becomes_an_absolute_moment(self) -> None:
        """`expires_in` is relative to the call, so only the caller can date it."""
        before = datetime.now(UTC)

        carried = stored().with_token(
            GoogleTokenResponse(access_token=ANY_ACCESS, expires_in=3599)
        )

        assert carried.expires_at is not None
        assert carried.expires_at.tzinfo is not None
        assert before + timedelta(seconds=3590) <= carried.expires_at


class TestRefreshAgainstAFakeTokenEndpoint:
    def test_it_trades_the_refresh_token_for_an_access_token(self, httpserver) -> None:
        httpserver.expect_request("/token", method="POST").respond_with_json(
            {
                "access_token": ISSUED,
                "expires_in": 3599,
                "scope": GMAIL_READONLY_SCOPE,
                "token_type": "Bearer",
            }
        )

        result = refreshed(stored(httpserver.url_for("/token")))

        assert result.access_token == ISSUED
        assert result.needs_refresh() is False

    def test_it_sends_the_grant_the_oauth_spec_names(self, httpserver) -> None:
        httpserver.expect_request("/token", method="POST").respond_with_json(
            {
                "access_token": ISSUED,
                "expires_in": 3599,
            }
        )

        refreshed(stored(httpserver.url_for("/token")))

        request, _ = httpserver.log[-1]
        assert request.form["grant_type"] == "refresh_token"
        assert request.form["client_id"] == CLIENT_ID
        assert request.form["client_secret"] == CLIENT_SECRET
        assert request.form["refresh_token"] == REFRESH_TOKEN

    def test_a_revoked_refresh_token_is_terminal(self, httpserver) -> None:
        """No amount of retrying fixes it — the account goes to `auth_error`."""
        httpserver.expect_request("/token", method="POST").respond_with_json(
            {
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            },
            status=400,
        )

        with pytest.raises(MailAuthError, match="expired or revoked"):
            refreshed(stored(httpserver.url_for("/token")))

    def test_a_rejected_client_is_terminal_too(self, httpserver) -> None:
        httpserver.expect_request("/token", method="POST").respond_with_json(
            {"error": "invalid_client"}, status=401
        )

        with pytest.raises(MailAuthError, match="invalid_client"):
            refreshed(stored(httpserver.url_for("/token")))

    def test_a_5xx_is_worth_trying_again(self, httpserver) -> None:
        httpserver.expect_request("/token", method="POST").respond_with_data(
            "upstream unavailable", status=503
        )

        with pytest.raises(MailTransientError, match="503"):
            refreshed(stored(httpserver.url_for("/token")))

    def test_a_rate_limit_carries_googles_own_retry_after(self, httpserver) -> None:
        httpserver.expect_request("/token", method="POST").respond_with_data(
            "slow down", status=429, headers={"Retry-After": "30"}
        )

        with pytest.raises(MailTransientError) as raised:
            refreshed(stored(httpserver.url_for("/token")))

        assert raised.value.retry_after == 30.0

    def test_a_retry_after_date_is_read_like_everywhere_else(self, httpserver) -> None:
        """An HTTP-date is legal there, so the floor is not thrown away.

        The token endpoint and the API are different Google front ends and do
        not agree on which form to send. Both go through the same parser now;
        a second, weaker one here used to discard the provider's own floor
        roughly half the time.
        """
        soon = format_datetime(datetime.now(UTC) + timedelta(seconds=45))
        httpserver.expect_request("/token", method="POST").respond_with_data(
            "slow down", status=429, headers={"Retry-After": soon}
        )

        with pytest.raises(MailTransientError) as raised:
            refreshed(stored(httpserver.url_for("/token")))

        assert raised.value.retry_after is not None
        assert 30.0 <= raised.value.retry_after <= 45.0

    def test_an_unreadable_retry_after_is_simply_no_floor(self, httpserver) -> None:
        httpserver.expect_request("/token", method="POST").respond_with_data(
            "slow down", status=429, headers={"Retry-After": "soon-ish"}
        )

        with pytest.raises(MailTransientError) as raised:
            refreshed(stored(httpserver.url_for("/token")))

        assert raised.value.retry_after is None

    def test_a_200_that_carries_no_token_is_not_a_success(self, httpserver) -> None:
        httpserver.expect_request("/token", method="POST").respond_with_data(
            "<html>a captive portal, not a token</html>", status=200
        )

        with pytest.raises(MailAuthError, match="no usable token"):
            refreshed(stored(httpserver.url_for("/token")))

    def test_an_endpoint_nobody_answers_is_transient(self) -> None:
        """A dropped network is the one case that is *not* the credential."""
        unreachable = f"http://127.0.0.1:{closed_port()}/token"

        with pytest.raises(MailTransientError, match="unreachable"):
            refreshed(stored(unreachable), timeout=2.0)


class TestTheAsyncWrapper:
    async def test_it_returns_what_the_blocking_call_returns(self, httpserver) -> None:
        httpserver.expect_request("/token", method="POST").respond_with_json(
            {
                "access_token": ISSUED,
                "expires_in": 3599,
            }
        )

        result = await refresh_async(
            stored(httpserver.url_for("/token")),
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )

        assert result.access_token == ISSUED

    async def test_it_keeps_the_blocking_call_off_the_event_loop(
        self, monkeypatch
    ) -> None:
        """§10 phase 3: the refresh blocks, so it belongs in `to_thread`."""
        seen: list[str] = []

        def record(credentials: GmailCredentials, **_: object) -> GmailCredentials:
            seen.append(threading.current_thread().name)
            return credentials

        monkeypatch.setattr(credentials_module, "refresh", record)
        loop_thread = threading.current_thread().name

        await refresh_async(stored(), client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

        assert seen, "the wrapper never called the blocking function"
        assert seen[0] != loop_thread, "it ran on the loop's own thread"


class TestTheErrorMessageCarriesNoCredential:
    """pydantic quotes its input, and here the input *is* the secret.

    The message from `from_secret` reaches four places, none of them
    encrypted: `mail_accounts.last_error`, `mail_sync_jobs.error`, the account
    page, and the log. Interpolating a `ValidationError` there copies a
    `client_secret` straight out of the Fernet column — the encryption is not
    broken, it is walked around.

    This is not a hypothetical path. Until the consent step was wired up, a
    stored Gmail credential was *always* missing its refresh token, so every
    press of "Connect" took it.
    """

    def test_a_missing_field_does_not_quote_what_was_stored(self) -> None:
        secret = json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})

        with pytest.raises(MailAuthError) as raised:
            GmailCredentials.from_secret(secret)

        assert CLIENT_SECRET not in str(raised.value)
        assert "input_value" not in str(raised.value)

    def test_a_wrong_type_does_not_quote_it_either(self) -> None:
        """`scopes` is what is malformed; the refresh token is what leaks."""
        secret = json.dumps({"refresh_token": REFRESH_TOKEN, "scopes": "not-a-list"})

        with pytest.raises(MailAuthError) as raised:
            GmailCredentials.from_secret(secret)

        assert REFRESH_TOKEN not in str(raised.value)
        assert "not-a-list" not in str(raised.value)

    def test_the_detail_is_still_on_the_traceback(self) -> None:
        """Kept for whoever is holding a debugger, off the sentence a human reads."""
        with pytest.raises(MailAuthError) as raised:
            GmailCredentials.from_secret("{}")

        assert raised.value.__cause__ is not None

    def test_the_message_says_what_to_do_about_it(self) -> None:
        with pytest.raises(MailAuthError, match="connect this mailbox again"):
            GmailCredentials.from_secret("{}")

    def test_a_token_endpoint_reply_is_not_quoted_back(self, httpserver) -> None:
        """The reply carries the access token, so the same rule applies."""
        leaked = "ya29.should-never-appear-in-a-message"
        httpserver.expect_request("/token", method="POST").respond_with_json(
            {
                "access_token": leaked,
                "expires_in": "not a number at all",
            }
        )

        with pytest.raises(MailAuthError) as raised:
            refreshed(stored(httpserver.url_for("/token")))

        assert leaked not in str(raised.value)
