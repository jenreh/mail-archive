"""The registry is how a provider reaches an engine that must not name one.

Small enough to read in a minute, which is the point: the alternative §3.1
rejected was a `MailSourceFactory` protocol, and the whole argument for a plain
callable is that a registration should be a line rather than a class. These
tests hold that line — a lambda is a valid factory here.
"""

import pytest

from mailarc_core.mail.model import (
    CredentialField,
    MailProvider,
    ProviderDescriptor,
)
from mailarc_sync.engine.registry import ProviderRegistry, UnknownProviderError

GMAIL = ProviderDescriptor(
    provider=MailProvider.GMAIL,
    label="Gmail",
    credential_fields=(
        CredentialField(name="refresh_token", label="Token", secret=True),
    ),
    supports_incremental=True,
)

IMAP = ProviderDescriptor(provider=MailProvider.IMAP, label="IMAP")


class StubSource:
    """Stands in for a real adapter; the registry never calls anything on it."""

    def __init__(self, account: object, secret: str) -> None:
        self.account = account
        self.secret = secret


@pytest.fixture
def registry() -> ProviderRegistry:
    return ProviderRegistry()


def test_a_lookup_hands_back_the_registered_factory(registry) -> None:
    registry.register(GMAIL, StubSource)

    factory = registry.factory_for(MailProvider.GMAIL)

    assert factory is StubSource


def test_the_factory_builds_a_source_from_an_account_and_a_secret(registry) -> None:
    """A plain callable, so a lambda in the composition root is a valid one."""
    registry.register(IMAP, lambda account, secret: StubSource(account, secret))  # noqa: PLW0108 - a lambda is exactly the point

    source = registry.factory_for(MailProvider.IMAP)("account-7", "decrypted")

    assert source.account == "account-7"
    assert source.secret == "decrypted"  # noqa: S105 - a fixture string, not one


def test_the_descriptor_comes_back_too(registry) -> None:
    """The account form renders its fields; one declaration serves both."""
    registry.register(GMAIL, StubSource)

    descriptor = registry.descriptor_for(MailProvider.GMAIL)

    assert descriptor.label == "Gmail"
    assert descriptor.credential_fields[0].secret is True


def test_the_descriptors_keep_the_order_they_were_registered_in(registry) -> None:
    registry.register(GMAIL, StubSource)
    registry.register(IMAP, StubSource)

    assert [one.provider for one in registry.descriptors()] == [
        MailProvider.GMAIL,
        MailProvider.IMAP,
    ]


def test_an_empty_registry_offers_nothing(registry) -> None:
    assert registry.descriptors() == ()
    assert registry.supports(MailProvider.GMAIL) is False


def test_registering_again_replaces_rather_than_raises(registry) -> None:
    """A composition root that runs twice after a reload says the same thing."""

    def other(account: object, secret: str) -> StubSource:
        return StubSource(account, secret)

    registry.register(GMAIL, StubSource)
    registry.register(GMAIL, other)

    assert registry.factory_for(MailProvider.GMAIL) is other
    assert len(registry.descriptors()) == 1


class TestAProviderNobodyRegistered:
    def test_the_factory_lookup_says_what_is_known(self, registry) -> None:
        """An account row naming a provider this process has not got is a
        configuration problem, and the message has to be usable as one."""
        registry.register(GMAIL, StubSource)

        with pytest.raises(UnknownProviderError) as error:
            registry.factory_for(MailProvider.M365)

        assert "m365" in str(error.value)
        assert "gmail" in str(error.value)

    def test_the_descriptor_lookup_refuses_the_same_way(self, registry) -> None:
        with pytest.raises(UnknownProviderError):
            registry.descriptor_for(MailProvider.FAKE)

    def test_supports_answers_without_raising(self, registry) -> None:
        """The UI asks before it offers; a question is not an error."""
        registry.register(GMAIL, StubSource)

        assert registry.supports(MailProvider.GMAIL) is True
        assert registry.supports(MailProvider.FAKE) is False


class TestTheConsentStep:
    """Some mailboxes need a second step between typing and working.

    Gmail's opens a browser; an IMAP app password is complete the moment it is
    typed. The account page has to tell those apart without naming either, so
    the registration carries the step and the page asks the registry.
    """

    @staticmethod
    async def _grant(values):
        return f"granted:{values['token']}"

    async def test_a_provider_can_register_one(self, registry) -> None:
        registry.register(GMAIL, StubSource, consent=self._grant)

        runner = registry.consent_for(MailProvider.GMAIL)

        assert runner is not None
        assert await runner({"token": "abc"}) == "granted:abc"

    def test_a_provider_without_one_says_so_rather_than_raising(self, registry) -> None:
        """ "This mailbox needs no browser" is an ordinary answer."""
        registry.register(IMAP, StubSource)

        assert registry.consent_for(MailProvider.IMAP) is None
        assert registry.needs_consent(MailProvider.IMAP) is False

    def test_needs_consent_is_what_the_page_branches_on(self, registry) -> None:
        registry.register(GMAIL, StubSource, consent=self._grant)
        registry.register(IMAP, StubSource)

        assert registry.needs_consent(MailProvider.GMAIL) is True
        assert registry.needs_consent(MailProvider.IMAP) is False

    def test_an_unregistered_provider_needs_nothing(self, registry) -> None:
        assert registry.needs_consent(MailProvider.FAKE) is False
        assert registry.consent_for(MailProvider.FAKE) is None

    def test_re_registering_without_one_drops_it(self, registry) -> None:
        """A reload has to be able to say something different, not add to it."""
        registry.register(GMAIL, StubSource, consent=self._grant)

        registry.register(GMAIL, StubSource)

        assert registry.consent_for(MailProvider.GMAIL) is None
