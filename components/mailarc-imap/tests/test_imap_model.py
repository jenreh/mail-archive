"""The descriptor, and the two rules that make it the whole of the UI change.

``ProviderDescriptor`` faces both ways: ``app/composition.py`` registers it and
``mailarc_ui.accounts.state`` renders ``credential_fields`` with ``rx.foreach``.
So the form for this provider is decided here, and the shape the form writes
back is decided here too — which is why the field names are pinned against the
names :class:`~mailarc_imap.source.credentials.ImapCredentials` reads, rather
than left to agree by habit.

:class:`~mailarc_imap.source.config.ImapConfig` gets its one test in this file
as well, and it is the §4.2 test: not one setting names an account, so a second
IMAP mailbox needs no second configuration.
"""

import pytest

from mailarc_core.mail.model import MailProvider, ProviderDescriptor
from mailarc_imap.source import (
    DEFAULT_FOLDER,
    GMAIL_ALL_MAIL,
    GMAIL_IMAP_HOST,
    ICLOUD_IMAP_HOST,
    IMAP_DESCRIPTOR,
    IMAPS_PORT,
    FolderListing,
    ImapConfig,
    ImapCredentials,
)


class TestTheDescriptor:
    """One declaration, so the form and the registry cannot drift apart."""

    def test_it_is_a_domain_value_object(self) -> None:
        assert isinstance(IMAP_DESCRIPTOR, ProviderDescriptor)
        assert IMAP_DESCRIPTOR.provider is MailProvider.IMAP

    def test_every_field_is_one_the_credential_reads(self) -> None:
        """A field the credential ignores is a box the user fills in for nothing."""
        declared = {field.name for field in IMAP_DESCRIPTOR.credential_fields}

        assert declared == set(ImapCredentials.model_fields)

    def test_the_password_is_the_only_secret(self) -> None:
        secret = {
            field.name for field in IMAP_DESCRIPTOR.credential_fields if field.secret
        }

        assert secret == {"password"}

    def test_the_two_fields_with_defaults_are_the_optional_ones(self) -> None:
        """``required=False`` is what lets the form send ``""`` and mean "default"."""
        optional = {
            field.name
            for field in IMAP_DESCRIPTOR.credential_fields
            if not field.required
        }

        assert optional == {"port", "folder"}

    def test_the_placeholders_name_the_defaults(self) -> None:
        placeholders = {
            field.name: field.placeholder for field in IMAP_DESCRIPTOR.credential_fields
        }

        assert placeholders["port"] == str(IMAPS_PORT)
        assert placeholders["folder"] == DEFAULT_FOLDER
        assert placeholders["host"] == ICLOUD_IMAP_HOST

    def test_the_gmail_folder_is_named_where_a_user_will_read_it(self) -> None:
        """Pointing Gmail at ``INBOX`` archives the inbox and nothing else."""
        folder = next(
            field
            for field in IMAP_DESCRIPTOR.credential_fields
            if field.name == "folder"
        )

        assert GMAIL_ALL_MAIL in folder.label

    def test_the_username_field_warns_that_it_must_be_the_account_address(
        self,
    ) -> None:
        """``_require_same_mailbox`` deletes the credential when the two differ.

        The UI compares ``verify()``'s address against the account row and wipes
        the stored password when they disagree. IMAP's only possible answer is
        the authenticated username, so a host that issues a username which is
        not an address turns a correctly filled form into a lost credential.
        The form is the one place this component can warn about it.
        """
        username = next(
            field
            for field in IMAP_DESCRIPTOR.credential_fields
            if field.name == "username"
        )

        assert "email address" in username.label

    def test_it_claims_a_delta(self) -> None:
        """``test_imap_source.TestTheDelta`` holds ``watermark()`` to this."""
        assert IMAP_DESCRIPTOR.supports_incremental is True

    def test_the_well_known_hosts_are_the_two_this_provider_covers(self) -> None:
        assert ICLOUD_IMAP_HOST == "imap.mail.me.com"
        assert GMAIL_IMAP_HOST == "imap.gmail.com"


class TestTheFolderListing:
    @pytest.mark.parametrize(
        ("flags", "selectable"),
        [
            ((), True),
            ((rb"\HasNoChildren",), True),
            ((rb"\Noselect",), False),
            ((rb"\noselect", rb"\HasChildren"), False),
        ],
    )
    def test_only_a_noselect_name_is_unselectable(
        self, flags: tuple[bytes, ...], selectable: bool
    ) -> None:
        assert FolderListing(name="x", flags=flags).selectable() is selectable


class TestTheConfig:
    """§4.2: a second account must not need a second config."""

    def test_not_one_setting_names_an_account(self) -> None:
        account_shaped = {"host", "port", "username", "password", "folder", "account"}

        assert not account_shaped & set(ImapConfig.model_fields)

    def test_its_defaults_are_usable_without_a_file(self) -> None:
        built = ImapConfig()

        assert built.page_size > 0
        assert built.connect_timeout > 0
        assert built.request_timeout >= built.connect_timeout

    def test_it_trusts_the_platform_by_default(self) -> None:
        """A private certificate authority is the exception, not the setup."""
        assert ImapConfig().tls_ca_file == ""

    def test_there_is_no_way_to_turn_tls_off(self) -> None:
        """An app password in the clear is the credential itself."""
        assert not [
            name
            for name in ImapConfig.model_fields
            if "tls" in name and "ca" not in name
        ]
