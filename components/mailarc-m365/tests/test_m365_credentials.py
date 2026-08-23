"""The blob, both of its shapes, and the refresh that must not lose a token.

Three failures are being prevented here and each of them is silent:

* A refresh response that omits a refresh token — the usual case — overwriting
  the stored one with `None`, which locks an account out on the *next*
  unattended run rather than on this one.
* A malformed blob raising a `ValidationError` whose message quotes its input.
  The input is the secret, and the destinations are `mail_accounts.last_error`,
  `mail_sync_jobs.error`, the page and the log, none of which are encrypted.
* An app-only credential pointed at the `common` authority, which Entra refuses
  with a code nobody reads and which cannot work by construction.

**No test here reaches `login.microsoftonline.com`.** MSAL is replaced at
`_application`, which is the only door to it.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from msal.exceptions import MsalError

from mailarc_core.mail.errors import MailAuthError, MailTransientError
from mailarc_m365.source import credentials as module
from mailarc_m365.source.credentials import (
    M365AppOnlyCredentials,
    M365DelegatedCredentials,
    M365FormValues,
    from_secret,
    mode_of,
    refresh,
)
from mailarc_m365.source.model import (
    APP_ONLY_SCOPES,
    DELEGATED_SCOPES,
    M365Mode,
)

TENANT = "contoso.onmicrosoft.com"
MAILBOX = "team@contoso.com"
STORED = "0.AR-stored-refresh-token"  # noqa: S105 - a fixture
ROTATED = "0.AR-rotated-refresh-token"  # noqa: S105 - a fixture
LIVE = "eyJ0.live"  # noqa: S105 - a fixture


class FakeApplication:
    """Stands in for `msal.PublicClientApplication` / `ConfidentialClient...`.

    Records what it was asked for, because the scopes are half of what is being
    tested: a delegated refresh that asked for `.default` or an app-only grant
    that named `Mail.Read` are both refused by Entra and by nothing else.
    """

    def __init__(self, result: Any = None, *, raises: Exception | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[tuple[Any, ...]] = []

    def acquire_token_by_refresh_token(
        self, refresh_token: str, scopes: list[str]
    ) -> Any:
        self.calls.append(("refresh_token", refresh_token, tuple(scopes)))
        if self.raises is not None:
            raise self.raises
        return self.result

    def acquire_token_for_client(self, scopes: list[str]) -> Any:
        self.calls.append(("client", tuple(scopes)))
        if self.raises is not None:
            raise self.raises
        return self.result


def install(monkeypatch: pytest.MonkeyPatch, application: FakeApplication) -> None:
    """Replace the one function in this component that reaches Microsoft."""
    monkeypatch.setattr(module, "_application", lambda *_args, **_kwargs: application)


def delegated(**overrides: Any) -> M365DelegatedCredentials:
    fields: dict[str, Any] = {"tenant_id": TENANT, "refresh_token": STORED} | overrides
    return M365DelegatedCredentials(**fields)


def app_only(**overrides: Any) -> M365AppOnlyCredentials:
    fields: dict[str, Any] = {"tenant_id": TENANT, "mailbox": MAILBOX} | overrides
    return M365AppOnlyCredentials(mode=M365Mode.APP_ONLY, **fields)


def assign(model: Any, field: str, value: object) -> None:
    """Write a field the way anything outside this test would, through setattr.

    Not a plain assignment: a frozen pydantic model exposes its fields as
    read-only properties, so `credentials.access_token = ...` is a *type* error
    and the type checker refuses the very statement whose runtime behaviour is
    the point.
    """
    setattr(model, field, value)


def refreshed(credentials: Any) -> Any:
    return refresh(
        credentials,
        client_id="a-client",
        client_secret="a-secret",
        authority=f"https://login.example.test/{TENANT}",
    )


class TestTheTwoShapes:
    def test_a_delegated_credential_reads_me(self) -> None:
        assert delegated().mailbox_path == "/me"

    def test_an_app_only_credential_reads_the_mailbox_it_names(self) -> None:
        assert app_only().mailbox_path == f"/users/{MAILBOX.replace('@', '%40')}"

    def test_an_address_with_a_hash_cannot_truncate_the_path(self) -> None:
        # A '#' is legal in a UPN synchronised from on-premises; unquoted it
        # would turn the rest of the URL into a fragment.
        path = app_only(mailbox="a#b@contoso.com").mailbox_path
        assert "#" not in path

    def test_app_only_refuses_the_shared_authorities(self) -> None:
        for tenant in ("common", "organizations", "consumers"):
            with pytest.raises(ValueError, match="never the shared"):
                M365AppOnlyCredentials(
                    mode=M365Mode.APP_ONLY, tenant_id=tenant, mailbox=MAILBOX
                )

    def test_both_are_frozen_so_a_refresh_cannot_be_forgotten(self) -> None:
        # A refresh produces a *new* object, so the caller cannot forget that a
        # rotated token has to go back into `mail_credentials`.
        for credentials in (delegated(), app_only()):
            with pytest.raises(ValueError, match="frozen"):
                assign(credentials, "access_token", LIVE)


class TestStaleness:
    def test_no_access_token_needs_a_refresh(self) -> None:
        assert delegated().needs_refresh() is True

    def test_a_token_expiring_within_the_leeway_is_already_stale(self) -> None:
        soon = datetime.now(UTC) + timedelta(seconds=5)
        assert delegated(access_token=LIVE, expires_at=soon).needs_refresh() is True

    def test_a_token_with_an_hour_left_is_used(self) -> None:
        later = datetime.now(UTC) + timedelta(hours=1)
        assert delegated(access_token=LIVE, expires_at=later).needs_refresh() is False

    def test_a_naive_expiry_is_read_as_utc_rather_than_raising(self) -> None:
        naive = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
        assert delegated(access_token=LIVE, expires_at=naive).needs_refresh() is False

    def test_asking_for_a_header_without_a_token_is_an_auth_error(self) -> None:
        with pytest.raises(MailAuthError, match="refresh before"):
            delegated().authorization_header()

    def test_the_header_is_a_bearer(self) -> None:
        assert delegated(access_token=LIVE).authorization_header() == f"Bearer {LIVE}"


class TestTheRefresh:
    def test_a_delegated_refresh_asks_with_the_stored_token_and_its_scopes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = FakeApplication({"access_token": LIVE, "expires_in": 3599})
        install(monkeypatch, application)

        refreshed(delegated())

        assert application.calls == [("refresh_token", STORED, DELEGATED_SCOPES)]

    def test_an_app_only_refresh_asks_for_default_and_carries_no_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = FakeApplication({"access_token": LIVE, "expires_in": 3599})
        install(monkeypatch, application)

        renewed = refreshed(app_only())

        assert application.calls == [("client", APP_ONLY_SCOPES)]
        assert renewed.access_token == LIVE

    def test_a_rotated_refresh_token_reaches_the_stored_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(
            monkeypatch,
            FakeApplication(
                {"access_token": LIVE, "expires_in": 3599, "refresh_token": ROTATED}
            ),
        )

        renewed = refreshed(delegated())

        assert json.loads(renewed.to_secret())["refresh_token"] == ROTATED

    def test_a_response_that_omits_one_does_not_blank_the_stored_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trap. Entra reissues a refresh token only now and then."""
        install(
            monkeypatch, FakeApplication({"access_token": LIVE, "expires_in": 3599})
        )

        renewed = refreshed(delegated())

        assert renewed.refresh_token == STORED
        assert json.loads(renewed.to_secret())["refresh_token"] == STORED

    def test_an_expiry_is_computed_from_now_because_entra_reports_a_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(
            monkeypatch, FakeApplication({"access_token": LIVE, "expires_in": 3599})
        )

        renewed = refreshed(delegated())

        assert renewed.expires_at is not None
        assert renewed.expires_at > datetime.now(UTC) + timedelta(minutes=50)

    def test_a_response_without_an_expiry_simply_has_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication({"access_token": LIVE}))

        assert refreshed(delegated()).expires_at is None

    def test_a_revoked_grant_is_an_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(
            monkeypatch,
            FakeApplication(
                {"error": "invalid_grant", "error_description": "AADSTS70000"}
            ),
        )

        with pytest.raises(MailAuthError, match="AADSTS70000"):
            refreshed(delegated())

    @pytest.mark.parametrize("code", ["temporarily_unavailable", "server_error"])
    def test_an_endpoint_having_a_bad_day_is_transient(
        self, monkeypatch: pytest.MonkeyPatch, code: str
    ) -> None:
        install(monkeypatch, FakeApplication({"error": code}))

        with pytest.raises(MailTransientError):
            refreshed(delegated())

    def test_a_token_response_that_does_not_validate_never_quotes_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same trap as `from_secret`, one layer further in.

        pydantic appends `input_value=` to every complaint, and the input here
        is the token endpoint's answer — an access token, and sometimes a
        freshly rotated refresh token. The destinations are
        `mail_accounts.last_error`, `mail_sync_jobs.error`, the page and the
        log, none of which are encrypted.
        """
        install(
            monkeypatch,
            # A `str` field handed a container: pydantic renders the whole
            # thing, secret and all, into the message it raises.
            FakeApplication({"access_token": [LIVE], "refresh_token": ROTATED}),
        )

        with pytest.raises(MailAuthError) as raised:
            refreshed(delegated())

        assert LIVE not in str(raised.value)
        assert ROTATED not in str(raised.value)

    def test_a_result_that_is_not_a_dict_at_all_is_still_one_of_the_four(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication(None))

        with pytest.raises(MailAuthError):
            refreshed(delegated())

    def test_msal_rejecting_our_input_is_a_configuration_fault(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication(raises=ValueError("reserved scope")))

        with pytest.raises(MailAuthError, match="Entra application"):
            refreshed(delegated())

    def test_an_msal_error_is_a_configuration_fault_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication(raises=MsalError("authority")))

        with pytest.raises(MailAuthError):
            refreshed(delegated())

    def test_a_socket_failure_underneath_msal_is_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `requests` raises its own exceptions; none of them may escape.
        install(monkeypatch, FakeApplication(raises=OSError("connection reset")))

        with pytest.raises(MailTransientError, match="unreachable"):
            refreshed(delegated())

    def test_no_msal_exception_escapes_this_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for error in (ValueError("x"), MsalError("y"), OSError("z"), TypeError("w")):
            install(monkeypatch, FakeApplication(raises=error))
            with pytest.raises((MailAuthError, MailTransientError)):
                refreshed(delegated())

    async def test_the_async_pair_puts_the_blocking_call_on_a_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeApplication({"access_token": LIVE, "expires_in": 60}))

        renewed = await module.refresh_async(
            delegated(),
            client_id="a-client",
            client_secret="a-secret",
            authority=f"https://login.example.test/{TENANT}",
        )

        assert renewed.access_token == LIVE


class TestBuildingTheApplication:
    def test_app_only_without_an_installation_secret_says_so(self) -> None:
        """Checked before MSAL is constructed, so nothing reaches the network."""
        with pytest.raises(MailAuthError, match="client secret"):
            module._application(
                app_only(),
                client_id="a-client",
                client_secret="",
                authority="https://login.example.test/t",
                timeout=1.0,
            )

    def test_that_sentence_survives_the_refresh_that_wraps_it(self) -> None:
        """The catch-all must not rewrite the taxonomy on its way past.

        `_application` is called inside `refresh`'s `try`, and a `MailError`
        is an `Exception`: without an `except MailError: raise` ahead of it,
        the "anything else came out of `requests`" branch turned the one
        actionable sentence in this module — set `app_m365_client_secret` —
        into "the token endpoint is unreachable". The engine would then back
        off and retry a configuration no waiting fixes, the account would
        never reach `auth_error`, and the UI would never offer the fix.
        """
        with pytest.raises(MailAuthError) as raised:
            refresh(
                app_only(),
                client_id="a-client",
                client_secret="",
                authority=f"https://login.example.test/{TENANT}",
            )

        assert not isinstance(raised.value, MailTransientError)
        assert "app_m365_client_secret" in str(raised.value)
        assert "unreachable" not in str(raised.value)


class TestReadingTheBlob:
    def test_what_to_secret_wrote_comes_back_as_the_same_shape(self) -> None:
        for original in (delegated(access_token=LIVE), app_only(access_token=LIVE)):
            assert from_secret(original.to_secret()) == original

    def test_the_account_forms_untouched_blob_is_a_delegated_credential(self) -> None:
        # Every value is a string and every unfilled box is "".
        secret = json.dumps(
            {"mode": "", "tenant_id": "", "mailbox": "", "refresh_token": STORED}
        )
        read = from_secret(secret)
        assert isinstance(read, M365DelegatedCredentials)
        assert read.tenant_id == "common"

    def test_an_app_only_form_blob_opens_without_a_consent_round_trip(self) -> None:
        secret = json.dumps(
            {"mode": "app-only", "tenant_id": TENANT, "mailbox": MAILBOX}
        )
        read = from_secret(secret)
        assert isinstance(read, M365AppOnlyCredentials)
        assert read.mailbox == MAILBOX

    def test_a_delegated_blob_without_a_refresh_token_is_not_a_credential(
        self,
    ) -> None:
        with pytest.raises(MailAuthError, match="connect this mailbox again"):
            from_secret(json.dumps({"mode": "delegated", "tenant_id": TENANT}))

    def test_an_app_only_blob_naming_the_common_authority_is_refused(self) -> None:
        with pytest.raises(MailAuthError):
            from_secret(
                json.dumps(
                    {"mode": "app-only", "tenant_id": "common", "mailbox": MAILBOX}
                )
            )

    def test_a_blob_that_is_not_json_fails_as_a_credential(self) -> None:
        with pytest.raises(MailAuthError, match="unreadable or incomplete"):
            from_secret("not json at all")

    def test_a_blob_that_is_json_but_not_an_object_fails_as_a_credential(self) -> None:
        with pytest.raises(MailAuthError):
            from_secret("[1, 2, 3]")

    def test_the_failure_never_quotes_the_secret(self) -> None:
        """`mail_accounts.last_error` is not an encrypted column."""
        leaky = f'{{"mode": "delegated", "refresh_token": "{STORED}", '
        with pytest.raises(MailAuthError) as raised:
            from_secret(leaky)
        assert STORED not in str(raised.value)
        assert leaky not in str(raised.value)

    def test_a_validation_failure_never_quotes_the_secret_either(self) -> None:
        leaky = json.dumps({"mode": "app-only", "tenant_id": "common", "x": STORED})
        with pytest.raises(MailAuthError) as raised:
            from_secret(leaky)
        assert STORED not in str(raised.value)


class TestTheMode:
    @pytest.mark.parametrize("written", ["", "  ", "delegated", "Delegated", "user"])
    def test_an_empty_or_delegated_box_means_delegated(self, written: str) -> None:
        assert mode_of({"mode": written}) is M365Mode.DELEGATED

    @pytest.mark.parametrize(
        "written", ["app-only", "APP-ONLY", "app_only", "apponly", "application"]
    )
    def test_every_spelling_microsoft_uses_means_app_only(self, written: str) -> None:
        assert mode_of({"mode": written}) is M365Mode.APP_ONLY

    def test_a_word_that_is_neither_is_refused_rather_than_assumed(self) -> None:
        # Signing in as the person when they asked for a service principal
        # would archive the wrong mailbox under the right name.
        with pytest.raises(MailAuthError, match="not a Microsoft 365 sign-in mode"):
            mode_of({"mode": "tenant"})

    def test_a_missing_key_means_delegated(self) -> None:
        assert mode_of({}) is M365Mode.DELEGATED


class TestTheFormValues:
    def test_it_reads_the_three_fields_and_the_consent_address(self) -> None:
        form = M365FormValues.read(
            {
                "mode": "app-only",
                "tenant_id": f"  {TENANT} ",
                "mailbox": " ",
                "email_address": MAILBOX,
            }
        )
        assert form.mode is M365Mode.APP_ONLY
        assert form.tenant_id == TENANT
        assert form.mailbox == ""
        assert form.email_address == MAILBOX

    def test_an_unset_tenant_falls_back_to_the_installations(self) -> None:
        assert M365FormValues.read({}).tenant_or("fallback-tenant") == "fallback-tenant"

    def test_an_unset_mailbox_falls_back_to_the_accounts_own_address(self) -> None:
        form = M365FormValues.read({"email_address": MAILBOX})
        assert form.mailbox_or_address() == MAILBOX

    def test_an_explicit_mailbox_wins_over_the_accounts_address(self) -> None:
        form = M365FormValues.read(
            {"mailbox": "shared@contoso.com", "email_address": MAILBOX}
        )
        assert form.mailbox_or_address() == "shared@contoso.com"
