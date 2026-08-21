"""The value objects, mostly for the one that does work: `EmailAddress`.

Normalising the address is what makes two sightings of the same human one
node, and the frozen-ness is what stops a page of results being edited after
the fact. Everything else here is a shape check that keeps the port honest.
"""

import pytest
from pydantic import ValidationError

from mailarc_core.mail.model import (
    AccountIdentity,
    CredentialField,
    EmailAddress,
    LabelInfo,
    LabelKind,
    MailProvider,
    MessagePage,
    MessageRef,
    ParsedMessage,
    ProviderDescriptor,
    RawMessage,
    SyncCursor,
    SyncCursorKind,
)


class TestEmailAddress:
    @pytest.mark.parametrize(
        "raw",
        [
            "bob@example.com",
            "Bob@Example.COM",
            "  bob@example.com  ",
            "<bob@example.com>",
            " <Bob@Example.com> ",
        ],
    )
    def test_every_spelling_lands_on_one_address(self, raw) -> None:
        assert EmailAddress(address=raw).address == "bob@example.com"

    def test_the_local_part_and_domain_are_readable_without_splitting_again(
        self,
    ) -> None:
        address = EmailAddress(address="bob.beispiel@mail.example.com")

        assert address.local_part == "bob.beispiel"
        assert address.domain == "mail.example.com"

    def test_an_address_with_no_at_sign_has_no_domain(self) -> None:
        """`Address.domain` is indexed; a malformed value must not fake one."""
        address = EmailAddress(address="postmaster")

        assert address.local_part == "postmaster"
        assert address.domain == ""

    def test_only_the_last_at_sign_splits(self) -> None:
        address = EmailAddress(address='"weird@name"@example.com')

        assert address.domain == "example.com"

    def test_two_sightings_of_one_address_are_the_same_value(self) -> None:
        """`participant_key` builds a set of these; equality has to hold."""
        first = EmailAddress(address="Bob@Example.com", display_name="Bob")
        second = EmailAddress(address="bob@example.com", display_name="Bob")

        assert first == second
        assert len({first, second}) == 1

    def test_the_display_name_stays_as_seen(self) -> None:
        """The graph collects every spelling; normalising here would lose them."""
        assert EmailAddress(address="b@x.de", display_name="Bob B.").display_name == (
            "Bob B."
        )

    def test_an_address_cannot_be_edited_afterwards(self) -> None:
        address = EmailAddress(address="bob@example.com")

        with pytest.raises(ValidationError):
            address.address = "carol@example.com"  # ty: ignore[invalid-assignment]


class TestMessageValues:
    def test_a_page_defaults_to_the_last_one(self) -> None:
        """No next cursor means the engine stops; that has to be the default."""
        page = MessagePage()

        assert page.refs == ()
        assert page.next_cursor is None
        assert page.estimated_total is None

    def test_a_cursor_carries_a_token_the_engine_never_reads(self) -> None:
        gmail_history_id = "historyId:98123"

        cursor = SyncCursor(provider=MailProvider.GMAIL, token=gmail_history_id)

        assert cursor.kind is SyncCursorKind.FULL
        assert cursor.token == gmail_history_id

    def test_a_raw_message_keeps_its_reference(self) -> None:
        ref = MessageRef(provider_message_id="17f", provider_thread_id="17a")
        raw = RawMessage(ref=ref, raw=b"From: a@b.de\n\nHallo.\n")

        assert raw.ref.provider_message_id == "17f"
        assert raw.raw.startswith(b"From:")

    def test_participants_are_sender_first_then_the_recipients(self) -> None:
        message = ParsedMessage(
            canonical_id="x@y.de",
            sender=EmailAddress(address="alice@example.com"),
            to=(EmailAddress(address="bob@example.com"),),
            cc=(EmailAddress(address="carol@partner.de"),),
        )

        assert [address.address for address in message.participants] == [
            "alice@example.com",
            "bob@example.com",
            "carol@partner.de",
        ]

    def test_a_message_without_a_sender_still_lists_its_recipients(self) -> None:
        message = ParsedMessage(
            canonical_id="x@y.de", to=(EmailAddress(address="bob@example.com"),)
        )

        assert len(message.participants) == 1


class TestProviderDescriptor:
    def test_one_declaration_feeds_the_registry_and_the_form(self) -> None:
        descriptor = ProviderDescriptor(
            provider=MailProvider.IMAP,
            label="IMAP",
            credential_fields=(
                CredentialField(name="host", label="Server", placeholder="imap.web.de"),
                CredentialField(name="password", label="Passwort", secret=True),
            ),
            supports_incremental=True,
        )

        assert descriptor.provider is MailProvider.IMAP
        assert [field.name for field in descriptor.credential_fields] == [
            "host",
            "password",
        ]
        assert descriptor.credential_fields[1].secret is True
        assert descriptor.credential_fields[0].required is True

    def test_a_provider_with_nothing_to_fill_in_is_allowed(self) -> None:
        """OAuth providers collect their credentials in a browser, not a form."""
        descriptor = ProviderDescriptor(provider=MailProvider.GMAIL, label="Gmail")

        assert descriptor.credential_fields == ()
        assert descriptor.supports_incremental is False


class TestLabels:
    def test_a_label_knows_where_it_came_from(self) -> None:
        label = LabelInfo(
            provider_label_id="Label_7", name="Projekte", kind=LabelKind.USER
        )

        assert label.kind is LabelKind.USER
        assert label.message_count is None

    def test_an_identity_carries_the_address_the_credentials_really_belong_to(
        self,
    ) -> None:
        identity = AccountIdentity(
            provider=MailProvider.GMAIL,
            address=EmailAddress(address="Alice@Example.COM"),
        )

        assert identity.address.address == "alice@example.com"
