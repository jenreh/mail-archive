"""The constants and the descriptor, pinned where getting them wrong is silent.

Three of these tests exist because the failure they prevent has no symptom
until a real mailbox is involved: MSAL raises on a reserved scope, a
client-credentials request is refused if it names a permission, and a
descriptor that promises a delta the watermark cannot deliver produces an
account the scheduler queues forever.
"""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from mailarc_core.mail.model import MailProvider
from mailarc_m365.source.model import (
    APP_ONLY_SCOPES,
    DELEGATED_SCOPES,
    GRAPH_DEFAULT_SCOPE,
    GRAPH_MAIL_READ_SCOPE,
    M365_DESCRIPTOR,
    GraphTokenError,
    GraphTokenResult,
    M365Mode,
    retry_after_seconds,
)

RESERVED = frozenset({"openid", "profile", "offline_access"})
"""MSAL adds these itself and raises `ValueError` on a caller that passes one."""

FIXTURE = "eyJ0.a-token"  # noqa: S105 - a fixture


class TestTheScopes:
    def test_the_delegated_scopes_name_none_of_msals_reserved_ones(self) -> None:
        assert RESERVED.isdisjoint(DELEGATED_SCOPES)

    def test_the_delegated_scopes_ask_for_mail_and_for_an_identity(self) -> None:
        # `verify()` has to say whose mailbox this is, and a Mail.Read token
        # alone is refused at GET /me.
        assert GRAPH_MAIL_READ_SCOPE in DELEGATED_SCOPES
        assert len(DELEGATED_SCOPES) == 2

    def test_app_only_asks_for_default_and_nothing_by_name(self) -> None:
        # A client-credentials request that names a permission is AADSTS1002012.
        assert APP_ONLY_SCOPES == (GRAPH_DEFAULT_SCOPE,)
        assert GRAPH_MAIL_READ_SCOPE not in APP_ONLY_SCOPES


class TestTheDescriptor:
    def test_it_is_this_provider_and_promises_a_delta(self) -> None:
        assert M365_DESCRIPTOR.provider is MailProvider.M365
        assert M365_DESCRIPTOR.supports_incremental is True

    def test_no_field_is_required_so_an_untouched_form_is_valid(self) -> None:
        # The account form writes every declared field, filled or not; a
        # required one would make "leave it empty for delegated" a lie.
        assert not any(field.required for field in M365_DESCRIPTOR.credential_fields)

    def test_no_field_is_a_secret_because_the_client_belongs_to_the_install(
        self,
    ) -> None:
        assert not any(field.secret for field in M365_DESCRIPTOR.credential_fields)

    def test_it_asks_for_the_three_things_only_the_user_knows(self) -> None:
        names = [field.name for field in M365_DESCRIPTOR.credential_fields]
        assert names == ["mode", "tenant_id", "mailbox"]

    def test_no_field_collides_with_the_consent_address_key(self) -> None:
        # `ConsentRunner` receives the account's address under that name, and a
        # credential field spelled the same would be overwritten by it.
        from mailarc_core.mail.ports import CONSENT_ADDRESS_KEY

        assert CONSENT_ADDRESS_KEY not in {
            field.name for field in M365_DESCRIPTOR.credential_fields
        }


class TestRetryAfter:
    def test_a_number_of_seconds_is_read_as_one(self) -> None:
        assert retry_after_seconds("30") == 30.0

    def test_an_http_date_is_read_as_the_seconds_until_it(self) -> None:
        later = datetime.now(UTC) + timedelta(seconds=45)
        seconds = retry_after_seconds(format_datetime(later))
        assert seconds is not None
        assert 30 <= seconds <= 60

    def test_a_date_in_the_past_is_no_floor_rather_than_a_negative_one(self) -> None:
        earlier = datetime.now(UTC) - timedelta(minutes=5)
        assert retry_after_seconds(format_datetime(earlier)) == 0.0

    def test_a_date_without_a_zone_is_read_as_utc_rather_than_raising(self) -> None:
        """Comparing a naive datetime to an aware `now` is a `TypeError`."""
        seconds = retry_after_seconds("Wed, 21 Oct 2099 07:28:00")
        assert seconds is not None
        assert seconds > 0

    @pytest.mark.parametrize("header", [None, "", "soon", "later today"])
    def test_anything_else_is_simply_no_floor(self, header: str | None) -> None:
        assert retry_after_seconds(header) is None


class TestTheTokenShapes:
    def test_a_result_without_a_refresh_token_says_so_rather_than_failing(
        self,
    ) -> None:
        issued = GraphTokenResult.model_validate(
            {"access_token": FIXTURE, "expires_in": 3599, "token_type": "Bearer"}
        )
        assert issued.refresh_token is None

    def test_extra_keys_msal_adds_do_not_break_validation(self) -> None:
        issued = GraphTokenResult.model_validate(
            {"access_token": FIXTURE, "id_token_claims": {}, "token_source": "identity"}
        )
        assert issued.access_token == FIXTURE

    @pytest.mark.parametrize("code", ["temporarily_unavailable", "server_error"])
    def test_the_two_rfc_codes_that_mean_wait_are_transient(self, code: str) -> None:
        assert GraphTokenError(error=code).transient() is True

    @pytest.mark.parametrize("code", ["invalid_grant", "invalid_client", ""])
    def test_every_other_refusal_is_a_credential(self, code: str) -> None:
        assert GraphTokenError(error=code).transient() is False

    def test_describe_joins_what_entra_said_and_nothing_else(self) -> None:
        described = GraphTokenError(
            error="invalid_grant", error_description="AADSTS70000"
        )
        assert described.describe() == "invalid_grant: AADSTS70000"


def test_the_two_modes_are_spelled_the_way_microsoft_spells_them() -> None:
    assert M365Mode.DELEGATED.value == "delegated"
    assert M365Mode.APP_ONLY.value == "app-only"
