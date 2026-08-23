"""One consent runner, two answers — and the browser half never reaches Entra.

MSAL is replaced at `_public_application`, which is the only door to
`login.microsoftonline.com`. The loopback server is **real**: the fake
application hands back an `auth_uri` that is the redirect URI itself, so the
stand-in browser visiting it does exactly what a browser does after a person
signs in, and the whole round trip is exercised without a network.

The app-only half is the interesting asymmetry. It registers a runner and then
does not open anything, because a service principal's permission was granted
once by an administrator, in the tenant, before this mailbox existed.
"""

import socket
from typing import Any

import pytest
from msal.exceptions import MsalError

from mailarc_core.mail.errors import MailAuthError
from mailarc_m365.source import oauth as module
from mailarc_m365.source.config import M365Config
from mailarc_m365.source.credentials import (
    M365AppOnlyCredentials,
    M365DelegatedCredentials,
    from_secret,
)
from mailarc_m365.source.model import DELEGATED_SCOPES
from mailarc_m365.source.oauth import consent_runner, run_consent

TENANT = "contoso.onmicrosoft.com"
MAILBOX = "team@contoso.com"
GRANTED = "0.AR-granted"  # noqa: S105 - a fixture
LIVE = "eyJ0.live"  # noqa: S105 - a fixture
RESERVED = frozenset({"openid", "profile", "offline_access"})

TOKEN: dict[str, Any] = {
    "access_token": LIVE,
    "refresh_token": GRANTED,
    "expires_in": 3599,
    "scope": "Mail.Read User.Read profile openid email",
}


class FakeApplication:
    """MSAL's public client, with the sign-in replaced by an instant redirect.

    ``auth_uri`` is the loopback address itself, so the stand-in browser's one
    GET is the redirect — which is what makes the real `LoopbackServer` part of
    this test rather than something mocked out beside it.
    """

    def __init__(self, result: Any = None, *, query: str = "code=THE-CODE&state=st"):
        self.result = TOKEN if result is None else result
        self.query = query
        self.flow: dict[str, Any] | None = None
        self.redeemed: tuple[Any, Any] | None = None

    def initiate_auth_code_flow(
        self,
        scopes: list[str],
        *,
        redirect_uri: str,
        prompt: str | None = None,
        login_hint: str | None = None,
    ) -> dict[str, Any]:
        self.flow = {
            "auth_uri": f"{redirect_uri}?{self.query}",
            "state": "st",
            "scopes": tuple(scopes),
            "redirect_uri": redirect_uri,
            "prompt": prompt,
            "login_hint": login_hint,
        }
        return self.flow

    def acquire_token_by_auth_code_flow(
        self, flow: dict[str, Any], response: Any
    ) -> Any:
        self.redeemed = (flow, response)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def install(monkeypatch: pytest.MonkeyPatch, application: FakeApplication) -> None:
    """Replace MSAL and the browser; the loopback server stays real."""
    monkeypatch.setattr(
        module, "_public_application", lambda *_args, **_kwargs: application
    )

    def browser(url: str) -> None:
        import urllib.request

        with urllib.request.urlopen(url, timeout=5) as answer:  # noqa: S310
            answer.read()

    monkeypatch.setattr(module, "_open_browser", browser)


def config(**overrides: Any) -> M365Config:
    settings: dict[str, Any] = {
        "client_id": "a-client",
        "authority_host": "http://127.0.0.1:1",
        "consent_timeout": 5.0,
    } | overrides
    return M365Config(**settings)


class TestTheDelegatedConsent:
    def test_it_stores_what_the_sign_in_earned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication())

        granted = run_consent(config(), tenant_id=TENANT)

        assert isinstance(granted, M365DelegatedCredentials)
        assert granted.refresh_token == GRANTED
        assert granted.access_token == LIVE
        assert granted.tenant_id == TENANT
        assert granted.expires_at is not None

    def test_it_asks_for_no_scope_msal_reserves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MSAL adds openid/profile/offline_access itself and raises ValueError
        # on a caller that passes one.
        application = FakeApplication()
        install(monkeypatch, application)

        run_consent(config())

        assert application.flow is not None
        assert application.flow["scopes"] == DELEGATED_SCOPES
        assert RESERVED.isdisjoint(application.flow["scopes"])

    def test_the_account_the_row_names_is_preselected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = FakeApplication()
        install(monkeypatch, application)

        run_consent(config(), login_hint="jens@contoso.com")

        assert application.flow is not None
        assert application.flow["login_hint"] == "jens@contoso.com"
        # The hint preselects; the chooser still opens, because a silent
        # sign-in as the wrong account archives somebody else's mail.
        assert application.flow["prompt"] == "select_account"

    def test_the_redirect_lands_on_the_loopback_port_that_was_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = FakeApplication()
        install(monkeypatch, application)

        run_consent(config())

        assert application.flow is not None
        assert application.flow["redirect_uri"].startswith("http://localhost:")
        assert application.redeemed is not None
        assert application.redeemed[1] == {"code": "THE-CODE", "state": "st"}

    def test_an_unconfigured_installation_says_so_before_a_window_opens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication())

        with pytest.raises(MailAuthError, match="not set up"):
            run_consent(config(client_id=""))

    def test_a_denied_consent_is_an_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication(query="error=access_denied"))

        with pytest.raises(MailAuthError, match="denied"):
            run_consent(config())

    def test_a_redirect_carrying_neither_code_nor_error_is_not_the_redirect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe — `/favicon.ico`, a preconnect — must not end the wait."""
        install(monkeypatch, FakeApplication(query="nothing=at-all"))

        with pytest.raises(MailAuthError, match="did not complete"):
            run_consent(config(consent_timeout=0.3))

    def test_an_abandoned_tab_becomes_an_auth_error_rather_than_a_hang(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nobody comes back at all: the deadline is what ends this.

        The browser is replaced with a no-op rather than with one that visits
        a URL the server answers 404 to. That distinction is the test: a
        stand-in browser raising `HTTPError` reaches the same sentence through
        the catch-all, so `RedirectTimeout` would never be exercised and a
        consent that really was abandoned would hang for `consent_timeout`
        with nothing proving it ever stops.
        """
        monkeypatch.setattr(
            module, "_public_application", lambda *_a, **_k: FakeApplication()
        )
        monkeypatch.setattr(module, "_open_browser", lambda _url: None)

        with pytest.raises(MailAuthError, match="no redirect within"):
            run_consent(config(consent_timeout=0.3))

    def test_a_loopback_port_already_in_use_keeps_its_own_sentence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_walk_the_browser`'s catch-all must not rewrite the taxonomy.

        `LoopbackServer.__enter__` raises a `MailAuthError` naming the port;
        without the `except MailAuthError: raise` ahead of the catch-all it
        would come back as the generic "did not complete", and a fixed port
        colliding with a second window would look like an abandoned tab.
        """
        install(monkeypatch, FakeApplication())
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]

            with pytest.raises(MailAuthError, match="already in use"):
                run_consent(config(loopback_port=port))

    def test_a_refusal_from_the_token_endpoint_is_an_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(
            monkeypatch,
            FakeApplication(
                {"error": "invalid_grant", "error_description": "AADSTS54005"}
            ),
        )

        with pytest.raises(MailAuthError, match="AADSTS54005"):
            run_consent(config())

    def test_a_state_mismatch_is_an_auth_error_and_not_a_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication(ValueError("state mismatch")))

        with pytest.raises(MailAuthError, match="could not be redeemed"):
            run_consent(config())

    def test_no_msal_exception_escapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for error in (MsalError("x"), OSError("y"), TypeError("z")):
            install(monkeypatch, FakeApplication(error))
            with pytest.raises(MailAuthError):
                run_consent(config())

    def test_a_grant_without_a_refresh_token_is_refused_now_not_at_3am(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication({"access_token": LIVE, "expires_in": 60}))

        with pytest.raises(MailAuthError, match="no refresh token"):
            run_consent(config())

    def test_a_token_dict_that_does_not_validate_never_quotes_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pydantic renders its input, and the input here is a fresh grant.

        The message ends up in `mail_accounts.last_error`, on the page and in
        the log, none of which are encrypted — so the sentence has to be the
        adapter's own, with the detail left on the traceback.
        """
        install(
            monkeypatch,
            # A `str` field handed a container: pydantic renders the whole
            # thing, refresh token and all, into the message it raises.
            FakeApplication({"access_token": [LIVE], "refresh_token": GRANTED}),
        )

        with pytest.raises(MailAuthError) as raised:
            run_consent(config())

        assert LIVE not in str(raised.value)
        assert GRANTED not in str(raised.value)

    def test_a_grant_narrowed_away_from_mail_is_no_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An administrator can restrict what a user may consent to, and the
        # sign-in then succeeds with less than was asked for.
        install(
            monkeypatch,
            FakeApplication(dict(TOKEN) | {"scope": "User.Read profile openid"}),
        )

        with pytest.raises(MailAuthError, match="without the mailbox permission"):
            run_consent(config())

    def test_entra_not_echoing_the_scopes_is_not_read_as_a_narrowed_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication(dict(TOKEN) | {"scope": None}))

        assert run_consent(config()).refresh_token == GRANTED

    async def test_the_async_pair_puts_the_blocking_half_on_a_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication())

        granted = await module.run_consent_async(config(), tenant_id=TENANT)

        assert granted.refresh_token == GRANTED


class TestTheAppOnlyPath:
    async def test_it_opens_no_browser_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def never(_url: str) -> None:
            raise AssertionError("app-only has nothing to consent to")

        monkeypatch.setattr(module, "_open_browser", never)
        runner = consent_runner(config(client_secret="a-secret"))

        secret = await runner(
            {"mode": "app-only", "tenant_id": TENANT, "mailbox": MAILBOX}
        )

        credentials = from_secret(secret)
        assert isinstance(credentials, M365AppOnlyCredentials)
        assert credentials.mailbox == MAILBOX
        assert credentials.tenant_id == TENANT

    async def test_the_accounts_own_address_stands_in_for_an_empty_mailbox(
        self,
    ) -> None:
        runner = consent_runner(config(client_secret="a-secret"))

        secret = await runner(
            {
                "mode": "app-only",
                "tenant_id": TENANT,
                "mailbox": "",
                "email_address": MAILBOX,
            }
        )

        credentials = from_secret(secret)
        assert isinstance(credentials, M365AppOnlyCredentials)
        assert credentials.mailbox == MAILBOX

    async def test_an_installation_without_a_client_secret_says_which_setting(
        self,
    ) -> None:
        runner = consent_runner(config())

        with pytest.raises(MailAuthError, match="app_m365_client_secret"):
            await runner({"mode": "app-only", "tenant_id": TENANT, "mailbox": MAILBOX})

    async def test_a_grant_with_no_mailbox_at_all_says_which_field(self) -> None:
        runner = consent_runner(config(client_secret="a-secret"))

        with pytest.raises(MailAuthError, match="Mailbox field"):
            await runner({"mode": "app-only", "tenant_id": TENANT})

    async def test_the_shared_authority_is_refused_before_entra_refuses_it(
        self,
    ) -> None:
        # A client-credentials token is issued *by* a tenant; `common` has none.
        runner = consent_runner(config(client_secret="a-secret"))

        with pytest.raises(MailAuthError, match="authority"):
            await runner({"mode": "app-only", "mailbox": MAILBOX})

    async def test_a_word_that_is_neither_mode_is_refused(self) -> None:
        runner = consent_runner(config(client_secret="a-secret"))

        with pytest.raises(MailAuthError, match="sign-in mode"):
            await runner({"mode": "tenant", "mailbox": MAILBOX})


class TestTheRunner:
    async def test_an_untouched_form_takes_the_delegated_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = FakeApplication()
        install(monkeypatch, application)
        runner = consent_runner(config())

        secret = await runner(
            {"mode": "", "tenant_id": "", "mailbox": "", "email_address": MAILBOX}
        )

        assert isinstance(from_secret(secret), M365DelegatedCredentials)
        assert application.flow is not None
        assert application.flow["login_hint"] == MAILBOX

    async def test_the_installations_tenant_is_used_when_the_form_names_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication())
        runner = consent_runner(config(default_tenant=TENANT))

        secret = await runner({"mode": "delegated"})

        assert from_secret(secret).tenant_id == TENANT

    async def test_what_it_returns_is_what_from_secret_reads_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The runner's return value goes straight into the encrypted column."""
        install(monkeypatch, FakeApplication())
        runner = consent_runner(config())

        secret = await runner({"email_address": MAILBOX})

        assert from_secret(secret).to_secret() == secret
