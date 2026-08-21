"""The browser flow is not driven here — what it is *handed* is, and what it waits on.

Running the real thing would need a browser, a human and Google, and the phase
3 DoD forbids the last of those outright. What can go wrong without any of them
is everything worth testing: a client configuration Google would reject, a
missing `access_type=offline` that quietly yields no refresh token, a consent
the user narrowed on Google's per-scope page, a tab nobody comes back from, and
a library exception escaping the adapter instead of arriving as a `MailAuthError`.

So `InstalledAppFlow` is replaced by a recorder, and the browser by a socket
that sends the redirect the way a browser would — to the real loopback server.
"""

import socket
import threading
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from mailarc_core.mail.errors import MailAuthError
from mailarc_google.source import oauth
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.credentials import GmailCredentials
from mailarc_google.source.model import (
    GMAIL_SCOPES,
    GOOGLE_AUTH_URI,
    GOOGLE_TOKEN_URI,
)
from mailarc_google.source.oauth import (
    WARNING_PAGE_HINT,
    run_consent,
    run_consent_async,
)

CLIENT_ID = "123456789-example.apps.googleusercontent.com"
CLIENT_SECRET = "the-users-own-client-secret"  # noqa: S105 - a fixture
GRANTED_REFRESH = "1//granted-refresh-token"  # noqa: S105 - a fixture
GRANTED_ACCESS = "ya29.granted"
FAKE_OAUTH_URL = "http://127.0.0.1:9/token"
NAIVE_EXPIRY = datetime(2030, 1, 1, 12, 0)  # noqa: DTZ001 - google-auth's own shape
STATE = "state-the-flow-generated"
CODE = "4/the-authorization-code"
ADDRESS = "travel@example.com"


def configured(**overrides: Any) -> GmailConfig:
    """A config with the installation's OAuth client set, and a short patience."""
    overrides.setdefault("consent_timeout", 3.0)
    return GmailConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, **overrides)


class Granted:
    """What google-auth hands back: naive UTC expiry and all."""

    def __init__(
        self,
        *,
        refresh_token: str | None = GRANTED_REFRESH,
        token: str | None = GRANTED_ACCESS,
        token_uri: str = GOOGLE_TOKEN_URI,
        scopes: list[str] | None = None,
        granted_scopes: list[str] | None = None,
        expiry: datetime | None = NAIVE_EXPIRY,
    ) -> None:
        self.refresh_token = refresh_token
        self.token = token
        self.token_uri = token_uri
        self.scopes = scopes if scopes is not None else list(GMAIL_SCOPES)
        self.granted_scopes = granted_scopes
        self.expiry = expiry


class FlowRecorder:
    """Stands in for `InstalledAppFlow` and remembers what it was told."""

    client_config: dict[str, Any]
    scopes: list[str]
    auth_kwargs: dict[str, Any]
    redirect_uri: str = ""
    exchanged: str = ""

    def __init__(
        self, granted: Granted | None = None, failure: Exception | None = None
    ) -> None:
        self.granted = granted or Granted()
        self.failure = failure
        self.thread_name = ""

    def from_client_config(
        self, client_config: dict[str, Any], scopes: list[str]
    ) -> FlowRecorder:
        self.client_config = client_config
        self.scopes = scopes
        return self

    def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
        self.auth_kwargs = kwargs
        self.thread_name = threading.current_thread().name
        return f"{GOOGLE_AUTH_URI}?state={STATE}", STATE

    def fetch_token(self, *, authorization_response: str) -> None:
        self.exchanged = authorization_response
        if self.failure is not None:
            raise self.failure

    @property
    def credentials(self) -> Granted:
        return self.granted


class Browser:
    """What the user does in the tab: comes back with a code, an error, or never."""

    def __init__(self, query: str | None = f"state={STATE}&code={CODE}") -> None:
        self.query = query
        self.opened: list[str] = []
        self.redirect_uri = ""

    def __call__(self, url: str) -> None:
        self.opened.append(url)
        if self.query is None:
            return
        target = urlparse(self.redirect_uri)
        assert target.hostname
        assert target.port
        with socket.create_connection((target.hostname, target.port), timeout=3) as s:
            s.sendall(
                f"GET /?{self.query} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
            )
            s.recv(256)


def install(
    monkeypatch, recorder: FlowRecorder, browser: Browser | None = None
) -> tuple[FlowRecorder, Browser]:
    """Wire recorder and browser together: the browser needs the bound port."""
    browser = browser or Browser()
    original_authorization_url = recorder.authorization_url

    def authorization_url(**kwargs: Any) -> tuple[str, str]:
        browser.redirect_uri = recorder.redirect_uri
        return original_authorization_url(**kwargs)

    recorder.authorization_url = authorization_url  # ty: ignore[invalid-assignment]
    monkeypatch.setattr(oauth, "InstalledAppFlow", recorder)
    monkeypatch.setattr(oauth, "_open_browser", browser)
    return recorder, browser


class TestWhatTheFlowIsHanded:
    def test_the_client_configuration_is_an_installed_app(self, monkeypatch) -> None:
        """A `web` client would make Google reject the loopback redirect."""
        recorder, _ = install(monkeypatch, FlowRecorder())

        run_consent(configured())

        assert set(recorder.client_config) == {"installed"}

    def test_it_carries_the_users_own_oauth_client(self, monkeypatch) -> None:
        recorder, _ = install(monkeypatch, FlowRecorder())

        run_consent(configured())

        installed = recorder.client_config["installed"]
        assert installed["client_id"] == CLIENT_ID
        assert installed["client_secret"] == CLIENT_SECRET
        assert installed["auth_uri"] == GOOGLE_AUTH_URI

    def test_consent_goes_to_googles_v2_authorization_endpoint(self) -> None:
        """Pinned, because the legacy /o/oauth2/auth endpoint still resolves."""
        assert GOOGLE_AUTH_URI == "https://accounts.google.com/o/oauth2/v2/auth"

    def test_the_token_endpoint_comes_from_configuration(self, monkeypatch) -> None:
        recorder, _ = install(monkeypatch, FlowRecorder())

        run_consent(configured(token_uri=FAKE_OAUTH_URL))

        assert recorder.client_config["installed"]["token_uri"] == FAKE_OAUTH_URL

    def test_it_asks_for_exactly_the_declared_scopes(self, monkeypatch) -> None:
        recorder, _ = install(monkeypatch, FlowRecorder())

        run_consent(configured())

        assert recorder.scopes == list(GMAIL_SCOPES)

    def test_it_asks_for_offline_access_or_there_is_no_refresh_token(
        self, monkeypatch
    ) -> None:
        """Without both of these Google issues a token that dies in an hour."""
        recorder, _ = install(monkeypatch, FlowRecorder())

        run_consent(configured())

        assert recorder.auth_kwargs["access_type"] == "offline"
        assert recorder.auth_kwargs["prompt"] == "consent"

    def test_the_mailbox_is_passed_as_the_login_hint(self, monkeypatch) -> None:
        """Skips Google's account chooser — and its wrong answer."""
        recorder, _ = install(monkeypatch, FlowRecorder())

        run_consent(configured(), login_hint=ADDRESS)

        assert recorder.auth_kwargs["login_hint"] == ADDRESS

    def test_without_a_mailbox_no_hint_is_sent(self, monkeypatch) -> None:
        recorder, _ = install(monkeypatch, FlowRecorder())

        run_consent(configured())

        assert "login_hint" not in recorder.auth_kwargs

    def test_the_redirect_names_the_port_the_server_bound(self, monkeypatch) -> None:
        recorder, browser = install(monkeypatch, FlowRecorder())

        run_consent(configured())

        parsed = urlparse(recorder.redirect_uri)
        assert parsed.scheme == "http"
        assert parsed.hostname == "localhost"
        assert parsed.port
        assert parsed.port > 0
        assert browser.opened == [f"{GOOGLE_AUTH_URI}?state={STATE}"]

    def test_a_configured_loopback_port_is_the_one_bound(self, monkeypatch) -> None:
        with socket.socket() as probe:
            probe.bind(("localhost", 0))
            free_port = probe.getsockname()[1]
        recorder, _ = install(monkeypatch, FlowRecorder())

        run_consent(configured(loopback_port=free_port))

        assert urlparse(recorder.redirect_uri).port == free_port

    def test_the_code_is_exchanged_with_the_state_google_sent_back(
        self, monkeypatch
    ) -> None:
        """oauthlib checks the state on this URL; it must be the browser's, verbatim."""
        recorder, _ = install(monkeypatch, FlowRecorder())

        run_consent(configured())

        exchanged = urlparse(recorder.exchanged)
        assert exchanged.scheme == "https", "oauthlib insists on it"
        assert parse_qs(exchanged.query) == {"state": [STATE], "code": [CODE]}


class TestWhatComesBack:
    def test_the_grant_becomes_a_storable_credential(self, monkeypatch) -> None:
        install(monkeypatch, FlowRecorder())

        granted = run_consent(configured())

        assert granted.refresh_token == GRANTED_REFRESH
        assert granted.access_token == GRANTED_ACCESS
        assert granted.scopes == GMAIL_SCOPES
        assert "client_secret" not in granted.to_secret()
        assert "client_id" not in granted.to_secret()

    def test_the_naive_expiry_google_auth_reports_is_read_as_utc(
        self, monkeypatch
    ) -> None:
        install(monkeypatch, FlowRecorder())

        granted = run_consent(configured())

        assert granted.expires_at == NAIVE_EXPIRY.replace(tzinfo=UTC)

    def test_a_grant_that_names_no_expiry_simply_refreshes_first(
        self, monkeypatch
    ) -> None:
        install(monkeypatch, FlowRecorder(granted=Granted(expiry=None)))

        granted = run_consent(configured())

        assert granted.expires_at is None
        assert granted.needs_refresh() is True

    def test_it_survives_the_round_trip_into_the_credential_column(
        self, monkeypatch
    ) -> None:
        install(monkeypatch, FlowRecorder())

        granted = run_consent(configured())

        assert GmailCredentials.from_secret(granted.to_secret()) == granted

    def test_what_google_actually_granted_is_what_gets_stored(
        self, monkeypatch
    ) -> None:
        """`granted_scopes` over `scopes`: the first is the answer, the second the ask."""
        wider = [*GMAIL_SCOPES, "openid"]
        install(monkeypatch, FlowRecorder(granted=Granted(granted_scopes=wider)))

        granted = run_consent(configured())

        assert granted.scopes == tuple(wider)


class TestWhenConsentDoesNotHappen:
    def test_a_grant_without_a_refresh_token_is_refused_now(self, monkeypatch) -> None:
        install(monkeypatch, FlowRecorder(granted=Granted(refresh_token=None)))

        with pytest.raises(MailAuthError, match="no refresh token"):
            run_consent(configured())

    def test_a_grant_the_user_narrowed_to_nothing_useful_is_refused(
        self, monkeypatch
    ) -> None:
        """Google's consent page has a checkbox per scope; unticked means unusable."""
        install(monkeypatch, FlowRecorder(granted=Granted(granted_scopes=["openid"])))

        with pytest.raises(MailAuthError, match="tick the Gmail checkbox"):
            run_consent(configured())

    def test_a_tab_nobody_comes_back_from_times_out_with_the_known_cure(
        self, monkeypatch
    ) -> None:
        """The listener must not wait forever, and the message must say what to do."""
        install(monkeypatch, FlowRecorder(), Browser(query=None))

        with pytest.raises(MailAuthError, match="did not complete") as caught:
            run_consent(configured(consent_timeout=0.3))

        assert WARNING_PAGE_HINT in str(caught.value)

    def test_the_user_pressing_cancel_is_a_denial(self, monkeypatch) -> None:
        install(
            monkeypatch,
            FlowRecorder(),
            Browser(query=f"state={STATE}&error=access_denied"),
        )

        with pytest.raises(MailAuthError, match="denied \\(access_denied\\)"):
            run_consent(configured())

    def test_a_library_exception_never_escapes_the_adapter(self, monkeypatch) -> None:
        """§7.6: an adapter raises from the taxonomy and nothing else."""
        install(
            monkeypatch,
            FlowRecorder(failure=RuntimeError("token endpoint said no")),
        )

        with pytest.raises(MailAuthError, match="did not complete"):
            run_consent(configured())

    def test_an_occupied_loopback_port_is_an_auth_error_too(self, monkeypatch) -> None:
        install(monkeypatch, FlowRecorder())
        with socket.socket() as taken:
            taken.bind(("localhost", 0))
            taken.listen()

            with pytest.raises(MailAuthError, match="in use"):
                run_consent(configured(loopback_port=taken.getsockname()[1]))

    def test_the_listener_is_gone_after_a_failure(self, monkeypatch) -> None:
        """An abandoned consent must not leave a port and a thread behind."""
        recorder, _ = install(monkeypatch, FlowRecorder(), Browser(query=None))
        with pytest.raises(MailAuthError):
            run_consent(configured(consent_timeout=0.3))

        port = urlparse(recorder.redirect_uri).port
        assert port
        with pytest.raises(OSError):
            socket.create_connection(("localhost", port), timeout=1)


class TestTheAsyncWrapper:
    async def test_it_keeps_the_blocking_flow_off_the_event_loop(
        self, monkeypatch
    ) -> None:
        """It waits for a human; on the loop's thread the UI would stop rendering."""
        recorder, _ = install(monkeypatch, FlowRecorder())
        loop_thread = threading.current_thread().name

        granted = await run_consent_async(configured(), login_hint=ADDRESS)

        assert granted.refresh_token == GRANTED_REFRESH
        assert recorder.thread_name != loop_thread
        assert recorder.auth_kwargs["login_hint"] == ADDRESS
