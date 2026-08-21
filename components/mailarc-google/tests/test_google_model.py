"""The descriptor is a promise to the account form and to the consent screen.

Two things here are worth a test and the rest is data. The credential fields
are what phase 4 renders with no UI code of its own, so a renamed field is a
form that silently stops collecting a secret. And the scope list is what the
user is asked to trust this application with — widening it is the kind of
change that should have to break a test first.
"""

import pytest
from pydantic import ValidationError

from mailarc_core.mail.model import MailProvider
from mailarc_google.source.model import (
    GMAIL_DESCRIPTOR,
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPES,
    GoogleTokenError,
    GoogleTokenResponse,
)

SAMPLE_ACCESS = "ya29.a0-example"
ANY_ACCESS = "t"
OTHER_ACCESS = "other"


class TestDescriptor:
    def test_it_is_the_registry_key_for_gmail(self) -> None:
        assert GMAIL_DESCRIPTOR.provider is MailProvider.GMAIL
        assert GMAIL_DESCRIPTOR.label == "Gmail"

    def test_it_asks_the_user_for_nothing(self) -> None:
        """The OAuth client belongs to the installation, not to the mailbox.

        It is configured once under `app.google`, so adding a Gmail account is
        an address and a button — nobody is sent to the Google Cloud console
        to make a project first. An empty tuple is the whole answer.
        """
        assert GMAIL_DESCRIPTOR.credential_fields == ()

    def test_it_does_not_ask_for_the_client_secret(self) -> None:
        """A field here would put the installation's secret in a browser form
        and a copy of it in every account row."""
        names = {field.name for field in GMAIL_DESCRIPTOR.credential_fields}

        assert "client_secret" not in names
        assert "client_id" not in names

    def test_it_does_not_ask_for_a_refresh_token(self) -> None:
        """Nobody types one in — consent earns it, and the form must not imply so."""
        names = {field.name for field in GMAIL_DESCRIPTOR.credential_fields}

        assert "refresh_token" not in names

    def test_it_claims_no_incremental_sync_yet(self) -> None:
        """Listing walks page tokens; the historyId delta is phase 7."""
        assert GMAIL_DESCRIPTOR.supports_incremental is False


MAILBOX_MUTATING_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.settings.basic",
        "https://www.googleapis.com/auth/gmail.settings.sharing",
    }
)
"""Every Gmail scope that can alter a message, a draft or a mail setting."""


class TestScopes:
    def test_it_asks_only_for_read_access_to_the_mailbox(self) -> None:
        assert GMAIL_SCOPES == (GMAIL_READONLY_SCOPE,)

    def test_readonly_keeps_raw_message_bodies_readable(self) -> None:
        """Losing readonly would fail every import at Google in production."""
        assert GMAIL_READONLY_SCOPE in GMAIL_SCOPES

    def test_no_scope_can_change_a_mailbox(self) -> None:
        """An archive that could modify or send would be asking for trust it
        has no code to use. Nothing here may touch mail itself.
        """
        assert not MAILBOX_MUTATING_SCOPES.intersection(GMAIL_SCOPES)


class TestGoogleTokenResponse:
    def test_it_reads_what_the_token_endpoint_actually_sends(self) -> None:
        issued = GoogleTokenResponse.model_validate(
            {
                "access_token": SAMPLE_ACCESS,
                "expires_in": 3599,
                "scope": GMAIL_READONLY_SCOPE,
                "token_type": "Bearer",
            }
        )

        assert issued.access_token == SAMPLE_ACCESS
        assert issued.expires_in == 3599
        assert issued.refresh_token is None, "a refresh usually reissues nothing"

    def test_it_ignores_fields_google_adds_later(self) -> None:
        issued = GoogleTokenResponse.model_validate(
            {
                "access_token": ANY_ACCESS,
                "id_token": "unused",
                "some_new_field": 1,
            }
        )

        assert issued.access_token == ANY_ACCESS

    def test_a_response_without_a_token_is_not_one(self) -> None:
        with pytest.raises(ValidationError):
            GoogleTokenResponse.model_validate({"expires_in": 3599})

    def test_it_cannot_be_edited_after_the_fact(self) -> None:
        issued = GoogleTokenResponse(access_token=ANY_ACCESS)

        with pytest.raises(ValidationError):
            issued.access_token = OTHER_ACCESS


class TestGoogleTokenError:
    def test_it_describes_the_refusal_in_one_line(self) -> None:
        refusal = GoogleTokenError(
            error="invalid_grant",
            error_description="Token has been expired or revoked.",
        )

        assert refusal.describe() == (
            "invalid_grant: Token has been expired or revoked."
        )

    def test_an_empty_envelope_describes_nothing(self) -> None:
        """Google is not obliged to explain; the status code still decided."""
        assert GoogleTokenError().describe() == ""
