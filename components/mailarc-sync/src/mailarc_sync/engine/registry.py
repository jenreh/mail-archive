"""Which providers exist, and how to build one — the engine's only vendor list.

The engine drives a mailbox through :class:`~mailarc_core.mail.ports.MailSourcePort`
and must not be able to name Gmail. This registry is where the names live
instead: ``app/composition.py`` registers every concrete provider at startup,
and everything below it asks by :class:`~mailarc_core.mail.model.MailProvider`.

A registration is a descriptor plus a callable, and deliberately not a factory
``Protocol`` (§3.1): the registry needs something it can call, and a one-method
interface around a callable buys nothing. The descriptor is the same object the
account form renders its fields from, so a new provider costs a registration
line and no UI change.
"""

import logging

from mailarc_core.mail.model import MailProvider, ProviderDescriptor
from mailarc_core.mail.ports import ConsentRunner, MailSourceFactory

logger = logging.getLogger(__name__)


class UnknownProviderError(LookupError):
    """Nothing is registered for this provider.

    Its own type because the caller can act on it: an account row naming a
    provider the process did not register is a configuration problem, not a
    mailbox problem, and the job fails with a sentence a human can use.
    """


class ProviderRegistry:
    """The providers this process knows, in the order they were registered.

    Order matters only for the UI — the account form lists them as they were
    registered, so the first one is the one a new user is offered.
    """

    def __init__(self) -> None:
        self._descriptors: dict[MailProvider, ProviderDescriptor] = {}
        self._factories: dict[MailProvider, MailSourceFactory] = {}
        self._consents: dict[MailProvider, ConsentRunner] = {}

    def register(
        self,
        descriptor: ProviderDescriptor,
        factory: MailSourceFactory,
        consent: ConsentRunner | None = None,
    ) -> None:
        """Make a provider available under the descriptor's own ``provider``.

        Registering twice replaces the entry rather than raising: a composition
        root that runs again after a reload has to be able to say the same
        thing twice.

        ``consent`` is the optional second step between typing a credential and
        owning a mailbox — OAuth needs a browser round trip, an app password
        does not. A provider that omits it is complete as soon as its fields
        are filled in, and the account page reads that off
        :meth:`needs_consent` rather than off the provider's name.
        """
        provider = descriptor.provider
        if provider in self._factories:
            logger.debug("Replacing the registration for %s", provider)
        self._descriptors[provider] = descriptor
        self._factories[provider] = factory
        self._consents.pop(provider, None)
        if consent is not None:
            self._consents[provider] = consent
        logger.debug(
            "Registered provider %s (%s), consent=%s",
            provider,
            descriptor.label,
            consent is not None,
        )

    def needs_consent(self, provider: MailProvider) -> bool:
        """Whether this provider has a second step before it can be used."""
        return provider in self._consents

    def consent_for(self, provider: MailProvider) -> ConsentRunner | None:
        """The provider's consent step, or ``None`` if it has none.

        ``None`` rather than an exception: "this mailbox needs no browser" is
        an ordinary answer, and the caller branches on it either way.
        """
        return self._consents.get(provider)

    def factory_for(self, provider: MailProvider) -> MailSourceFactory:
        """The callable that builds a source for this provider."""
        factory = self._factories.get(provider)
        if factory is None:
            raise UnknownProviderError(
                f"no provider registered for {provider.value!r} — "
                f"known: {sorted(one.value for one in self._factories)}"
            )
        return factory

    def descriptor_for(self, provider: MailProvider) -> ProviderDescriptor:
        """What this provider is called and which credentials it needs."""
        descriptor = self._descriptors.get(provider)
        if descriptor is None:
            raise UnknownProviderError(f"no provider registered for {provider.value!r}")
        return descriptor

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        """Every registered provider, for the account form to render."""
        return tuple(self._descriptors.values())

    def supports(self, provider: MailProvider) -> bool:
        """Whether this process can build a source for that provider."""
        return provider in self._factories
