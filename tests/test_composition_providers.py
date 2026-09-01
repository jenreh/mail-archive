"""The composition root's provider registry: which mailboxes this build can open.

Its own module and not part of ``test_composition.py`` for the reason its
sibling ``test_composition_semantic.py`` gives: that file was 1145 lines with
this in it, and §5 caps a file at 1000. The split is along a seam rather than
at a line number — everything here is about the one question of which mail
providers this build ships, what each one is bound to, and which of them have a
second step, which is the only part of the composition root that builds
something a network is on the other end of.

That seam is also why the in-process IMAP server, the throwaway CA and the two
canned HTTP stand-ins are here and nowhere else. They exist so that
``watermark()`` can be asked of every registered provider for real, and nothing
on the other side of the split has a mailbox in it.

The fixtures are this module's own rather than imported from its neighbour. A
pytest fixture is resolved by where it is defined, so sharing them would mean a
``tests/conftest.py`` whose autouse cache-clearing then applied to every module
under ``tests/`` — a wider change than the split is worth.
"""

import json
import logging
import re
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from appkit_commons.registry import service_registry

from app import composition
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.model import MailProvider
from mailarc_core.mail.ports import CONSENT_ADDRESS_KEY
from mailarc_google import GmailSource
from mailarc_google.source import GmailConfig, GmailCredentials
from mailarc_imap import ImapSource
from mailarc_imap.source import ImapConfig
from mailarc_m365 import M365Source
from mailarc_m365.source import M365Config, M365DelegatedCredentials, M365Mode
from mailarc_sync.engine import FakeMailSource, ProviderRegistry
from mailarc_ui.accounts import provider_registry as registry_the_ui_sees

IMAP_TEST_SUPPORT = (
    Path(__file__).resolve().parents[1] / "components" / "mailarc-imap" / "tests"
)
"""Where the IMAP component keeps its in-process server and its throwaway CA.

Borrowed rather than copied, and that needs saying because a root test reaching
into a component's test tree is not a thing this repository does anywhere else.
The alternative was a second IMAP server in ``tests/`` — four hundred lines of
protocol whose only job is to let ``watermark()`` answer once — and a second one
would be the one that drifts. ``mailarc-imap``'s is a real IMAP4rev1 server over
a real TLS socket, which is exactly the fidelity this file needs: IMAP has no
HTTP stand-in, so ``pytest-httpserver`` cannot serve it the way it serves the
other two providers.

Appended rather than prepended: pytest puts this directory on ``sys.path``
itself when it collects that component, and taking priority over the rest of the
path for a module named ``tls`` would be a poor trade for nothing.
"""

if str(IMAP_TEST_SUPPORT) not in sys.path:
    sys.path.append(str(IMAP_TEST_SUPPORT))

from imap_server import FakeImapServer
from tls import LoopbackCertificate

REGISTRY_LOGGER = "appkit_commons.registry"
"""Who says "overwriting" when a registration replaces one that was there."""


GMAIL_SECRET = GmailCredentials(refresh_token="refresh").to_secret()
"""A stored Gmail credential, in the shape the factory reads it back from.

A refresh token and nothing else: the OAuth client belongs to the installation
and is read from ``app.google``, not copied into every account row.
"""

GMAIL_API_ROOT = "/gmail/v1"
"""Where the local stand-in serves Gmail's API — never `googleapis.com`."""

GMAIL_TOKEN_PATH = "/token"  # noqa: S105 - a URL path

M365_API_ROOT = "/graph/v1.0"
"""Where the local stand-in serves Graph — never `graph.microsoft.com`."""

M365_DELTA_PATH = f"{M365_API_ROOT}/me/mailFolders/allitems/messages/delta"
"""The one call `watermark()` makes, spelled out rather than built.

`allitems` is `M365Config.delta_folder`'s default and `/me` is what a delegated
credential resolves to, so a change to either turns this into an unmatched
request the local server answers with a 500 — which is the point. A path built
from the same helpers the adapter uses would agree with it by construction and
prove nothing.
"""

M365_SECRET = M365DelegatedCredentials(
    refresh_token="0.AR-stored",
    # A live access token, so nothing reaches for MSAL: `needs_refresh()` is
    # false and the token endpoint is never dialled. The delegated shape is the
    # one a desktop installation gets; app-only is exercised by the component.
    access_token="eyJ0.live",
    expires_at=datetime.now(UTC) + timedelta(minutes=60),
).to_secret()
"""A stored Microsoft 365 credential, in the shape the factory reads it back from.

A refresh token and a cached access token, and no client: the Entra application
belongs to the installation and is read from ``app.m365``, exactly as Gmail's
OAuth client is.
"""

IMAP_FOLDER = "INBOX"
"""The one folder name RFC 3501 requires every server to have.

Spelled out rather than left empty: an IMAP account syncs exactly one folder, and
a fixture that relied on the adapter's default would not show that the field
travels in the secret.
"""

IMAP_FORM_SECRET = json.dumps(
    {
        "host": "imap.example.invalid",
        "port": "993",
        "username": "jens@example.invalid",
        "password": "app-specific-password",
        "folder": IMAP_FOLDER,
    }
)
"""A stored IMAP credential for the tests that never open a socket.

``.invalid`` is reserved by RFC 2606 and resolves nowhere, so a regression that
made building a source connect fails as a DNS error rather than by dialling
whatever ``imap.example.com`` happens to be.
"""


def imap_secret(server: FakeImapServer) -> str:
    """What the account form writes into ``mail_credentials.secret`` for IMAP.

    Not what ``ImapCredentials.to_secret()`` writes, deliberately. IMAP is the
    first provider with **no consent runner**, so nothing mints its secret: the
    account page serialises the descriptor's own ``credential_fields`` with
    ``json.dumps``, and every value in that mapping is a **string** — the port
    included, because an HTML input has no integers. This is the shape that
    reaches ``ImapSource.using(...)`` in the running application, so it is the
    shape the composition test hands it.
    """
    return json.dumps(
        {
            "host": "127.0.0.1",
            "port": str(server.port),
            "username": server.username,
            "password": server.password,
            "folder": IMAP_FOLDER,
        }
    )


@pytest.fixture(scope="session")
def imap_certificate(tmp_path_factory: pytest.TempPathFactory) -> LoopbackCertificate:
    """One throwaway CA for the whole run — minting per test buys no coverage."""
    return LoopbackCertificate(tmp_path_factory.mktemp("composition-imap-tls"))


@pytest.fixture
async def imap_server(
    imap_certificate: LoopbackCertificate,
) -> AsyncIterator[FakeImapServer]:
    """A listening IMAP server with an empty ``INBOX``, stopped afterwards.

    Empty is enough for everything this file asks of it: ``watermark()`` reads
    ``UIDVALIDITY`` and ``UIDNEXT`` off ``EXAMINE`` and never looks at a
    message. What the mailbox holds is the component's own suite's business.
    """
    fake = FakeImapServer(imap_certificate.server_context())
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture(autouse=True)
def _clear_caches():
    """The composition root memoises; each test needs a clean slate.

    ``_semantic_override`` is module state rather than a cache and is reset the
    same way: a test that adopted stored settings would otherwise decide what
    every later test's ``semantic_config()`` answers.
    """
    composition._semantic_override = None
    composition.graph_server.cache_clear()
    composition.provider_registry.cache_clear()
    composition.archive_reader.cache_clear()
    composition.analytics_reader.cache_clear()
    composition.semantic_embedder.cache_clear()
    composition.semantic_search.cache_clear()
    composition.sync_worker.cache_clear()
    yield
    composition._semantic_override = None
    composition.graph_server.cache_clear()
    composition.provider_registry.cache_clear()
    composition.archive_reader.cache_clear()
    composition.analytics_reader.cache_clear()
    composition.semantic_embedder.cache_clear()
    composition.semantic_search.cache_clear()
    composition.sync_worker.cache_clear()


def test_the_registry_offers_every_provider_this_build_ships() -> None:
    """All four, in registration order — that is the order the account form
    lists, so the first one is what a new user is offered.

    An exact list rather than a subset, and the assertion is as much about
    order as about membership: appending is how a provider joins, and a
    registration that inserted itself above Gmail would silently change what
    the account page offers first without failing anything else.
    """
    registry = composition.provider_registry()

    assert [one.provider for one in registry.descriptors()] == [
        MailProvider.FAKE,
        MailProvider.GMAIL,
        MailProvider.IMAP,
        MailProvider.M365,
    ]


async def test_every_provider_agrees_with_its_own_descriptor(
    monkeypatch, httpserver, tmp_path, imap_server, imap_certificate
) -> None:
    """``supports_incremental`` and ``watermark()`` are one statement in two places.

    Each provider pins the pairing for itself, but only this module knows the
    whole list, and only a walk over it catches the *next* provider. The
    consequence of a disagreement is the one the port's docstring names:
    ``IntervalScheduler`` queues that mailbox a delta every interval, the engine
    bootstraps into nothing every time, the job row says succeeded — and no
    component is in a position to notice, because each of them is right about
    its own half.

    Three of the four watermarks are a round trip, so all three are pointed at
    something local: Gmail and Microsoft 365 at a ``pytest-httpserver`` serving
    canned replies, IMAP at the component's own in-process server over TLS.
    **Nothing here reaches Google, Microsoft, or a mail host.**
    """
    _serve_gmail(httpserver)
    _serve_m365(httpserver)
    monkeypatch.setattr(
        composition,
        "google_config",
        lambda: GmailConfig(
            api_base_url=httpserver.url_for(GMAIL_API_ROOT),
            token_uri=httpserver.url_for(GMAIL_TOKEN_PATH),
            request_timeout=5.0,
        ),
    )
    monkeypatch.setattr(
        composition,
        "imap_config",
        lambda: ImapConfig(
            connect_timeout=5.0,
            request_timeout=5.0,
            tls_ca_file=imap_certificate.ca_file,
        ),
    )
    monkeypatch.setattr(
        composition,
        "m365_config",
        lambda: M365Config(
            client_id="a-client",
            api_base_url=httpserver.url_for(M365_API_ROOT),
            # Never dialled — the stored access token is live — and pointed at a
            # closed port so a regression that did dial fails instead of
            # reaching login.microsoftonline.com.
            authority_host="http://127.0.0.1:1",
            request_timeout=5.0,
        ),
    )
    mailbox = tmp_path / "exported"
    mailbox.mkdir()
    secrets = {
        MailProvider.FAKE: str(mailbox),
        # An access token that is still valid *and* a local token endpoint: the
        # first means no refresh is attempted, the second means a refresh could
        # not leave this machine if one were.
        MailProvider.GMAIL: GmailCredentials(
            refresh_token="refresh",
            token_uri=httpserver.url_for(GMAIL_TOKEN_PATH),
            access_token="local",
            expires_at=datetime.now(UTC) + timedelta(minutes=60),
        ).to_secret(),
        MailProvider.IMAP: imap_secret(imap_server),
        MailProvider.M365: M365_SECRET,
    }
    registry = composition.provider_registry()

    assert {one.provider for one in registry.descriptors()} == set(secrets), (
        "a registered provider has no fixture credential here — add one rather "
        "than letting the walk below skip it"
    )
    for descriptor in registry.descriptors():
        source = registry.factory_for(descriptor.provider)(
            None, secrets[descriptor.provider]
        )
        try:
            mark = await source.watermark()
        finally:
            await source.aclose()
        assert (mark is not None) is descriptor.supports_incremental, (
            f"{descriptor.provider} promises one thing and answers another"
        )


def _serve_gmail(httpserver) -> None:
    """Gmail's profile call and its token endpoint, on a loopback port."""
    httpserver.expect_request(f"{GMAIL_API_ROOT}/users/me/profile").respond_with_json(
        {"emailAddress": "jens@example.com", "historyId": "918273"}
    )
    httpserver.expect_request(GMAIL_TOKEN_PATH).respond_with_json(
        {"access_token": "token", "expires_in": 3600, "token_type": "Bearer"}
    )


def _serve_m365(httpserver) -> None:
    """One delta page that is already the end of its chain.

    A ``deltaLink`` and no ``nextLink``, so ``watermark()`` returns on the first
    page instead of draining. Enough for the question this file asks — does a
    mark come back at all — and the paging itself is the component's own suite's
    business.
    """
    httpserver.expect_request(M365_DELTA_PATH).respond_with_json(
        {
            "value": [],
            "@odata.deltaLink": httpserver.url_for(
                f"{M365_DELTA_PATH}?$deltatoken=minted"
            ),
        }
    )


def test_the_registry_can_build_the_fake_mailbox() -> None:
    built = composition.provider_registry().factory_for(MailProvider.FAKE)(
        None, "/mailboxes/exported"
    )

    assert isinstance(built, FakeMailSource)


async def test_the_registry_can_build_a_real_gmail_mailbox() -> None:
    """This is the only module allowed to name Gmail (§4.1), so a missing wire
    here shows up as "no provider registered" at the first import and nowhere
    earlier. The descriptor has to be Gmail's own as well: it is what the
    account form renders its credential fields from.
    """
    registry = composition.provider_registry()

    assert registry.descriptor_for(MailProvider.GMAIL) is GmailSource.DESCRIPTOR

    built = registry.factory_for(MailProvider.GMAIL)(None, GMAIL_SECRET)
    try:
        assert isinstance(built, GmailSource)
    finally:
        await built.aclose()


async def test_gmail_is_built_from_the_registered_configuration(monkeypatch) -> None:
    """Bound to this application's config, not to the environment.

    ``GmailSource.create`` would read a fresh ``GmailConfig()`` per call, which
    would leave the one module that builds from configuration out of the loop.
    """
    config = GmailConfig(api_base_url="https://gmail.test/v1")
    monkeypatch.setattr(composition, "google_config", lambda: config)

    built = composition.provider_registry().factory_for(MailProvider.GMAIL)(
        None, GMAIL_SECRET
    )
    try:
        assert isinstance(built, GmailSource)
        assert built._config is config
    finally:
        await built.aclose()


async def test_the_registry_can_build_a_real_imap_mailbox() -> None:
    """Same wire as Gmail's, and the same failure if it is missing.

    The descriptor has to be IMAP's own object: it is what the account form
    renders its five fields from, and IMAP is the first provider whose form is
    not empty — a descriptor built somewhere else would produce a form nobody
    could fill in. Nothing connects here; ``ImapClient`` dials on the first
    command, not in its constructor.
    """
    registry = composition.provider_registry()

    assert registry.descriptor_for(MailProvider.IMAP) is ImapSource.DESCRIPTOR

    built = registry.factory_for(MailProvider.IMAP)(None, IMAP_FORM_SECRET)
    try:
        assert isinstance(built, ImapSource)
    finally:
        await built.aclose()


async def test_imap_is_built_from_the_registered_configuration(monkeypatch) -> None:
    """Bound to this application's config, not to the environment.

    ``ImapSource.create`` would read a fresh ``ImapConfig()`` per call, which
    would leave the one module that builds from configuration out of the loop —
    and for IMAP the setting that would go missing is ``tls_ca_file``, i.e. the
    certificate authority a self-hosted mail server is verified against.
    """
    config = ImapConfig(page_size=7, tls_ca_file="/etc/ssl/private-ca.pem")
    monkeypatch.setattr(composition, "imap_config", lambda: config)

    built = composition.provider_registry().factory_for(MailProvider.IMAP)(
        None, IMAP_FORM_SECRET
    )
    try:
        assert isinstance(built, ImapSource)
        assert built._config is config
    finally:
        await built.aclose()


async def test_the_registry_can_build_a_real_m365_mailbox() -> None:
    """Same wire as Gmail's and IMAP's, from the stored delegated credential."""
    registry = composition.provider_registry()

    assert registry.descriptor_for(MailProvider.M365) is M365Source.DESCRIPTOR

    built = registry.factory_for(MailProvider.M365)(None, M365_SECRET)
    try:
        assert isinstance(built, M365Source)
    finally:
        await built.aclose()


async def test_m365_is_built_from_the_registered_configuration(monkeypatch) -> None:
    """Bound to this application's config, and here that matters most.

    ``M365Config`` carries the Entra client secret an app-only grant presents,
    and ``api_base_url`` — which is also the origin fence a stored cursor is
    checked against, because this is the only provider whose cursor is a whole
    URL. A source that read its own ``M365Config()`` off the environment would
    answer both questions somewhere else.
    """
    config = M365Config(client_id="a-client", api_base_url="https://graph.test/v1.0")
    monkeypatch.setattr(composition, "m365_config", lambda: config)

    built = composition.provider_registry().factory_for(MailProvider.M365)(
        None, M365_SECRET
    )
    try:
        assert isinstance(built, M365Source)
        assert built._config is config
    finally:
        await built.aclose()


def test_which_providers_have_a_second_step() -> None:
    """Consent is a fact about the provider, and all four answers are load-bearing.

    A folder of ``.eml`` files needs no browser and neither does IMAP — an app
    password is complete the moment it is typed, and IMAP is the path that
    proves a provider with no runner works end to end. Gmail and Microsoft 365
    both have one; Microsoft 365's is registered even though one of its two
    modes opens nothing, because the account page reads "does this provider
    have a second step" off the registration and the answer for the mode most
    people use is yes.
    """
    registry = composition.provider_registry()

    assert {one: registry.needs_consent(one) for one in MailProvider} == {
        MailProvider.FAKE: False,
        MailProvider.GMAIL: True,
        MailProvider.IMAP: False,
        MailProvider.M365: True,
    }


def test_the_registry_is_a_singleton() -> None:
    """A second registry would be a second answer to "which providers exist"."""
    assert composition.provider_registry() is composition.provider_registry()


@pytest.fixture
def _published_registry():
    """Publishing writes into the process-wide registry; put it back after."""
    registry = service_registry()
    saved = registry.snapshot()
    yield
    registry.restore(saved)


@pytest.mark.usefixtures("_published_registry")
def test_the_ui_finds_the_registry_without_importing_the_app() -> None:
    """`mailarc-ui` is a component and may not import `app` (§4.1), so the
    providers are left in the service registry for it — the same route every
    configuration takes. This asserts through the UI's own lookup, because
    that is the code a broken hand-over would break."""
    published = composition.publish_provider_registry()

    assert published is composition.provider_registry()
    assert registry_the_ui_sees() is published


@pytest.mark.usefixtures("_published_registry")
def test_publishing_twice_leaves_one_registry(caplog) -> None:
    """The application can be reloaded, so the second pass has to be a no-op:
    the same list, and nothing in the log about overwriting it that would make
    a reader wonder whether there are now two."""
    first = composition.publish_provider_registry()

    with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
        assert composition.publish_provider_registry() is first

    assert service_registry().get(ProviderRegistry) is first
    assert caplog.records == []


class TestTheGmailConsent:
    """The browser half of connecting a mailbox, registered where it belongs.

    ``mailarc-ui`` may not import a provider (§4.1), so the account page asks
    the registry whether this provider has a consent step and runs whatever it
    finds. Which makes this module — the only one allowed to name Gmail — the
    only place the OAuth client is read.
    """

    @staticmethod
    def _config(**overrides) -> GmailConfig:
        return GmailConfig(
            client_id="123-example.apps.googleusercontent.com",
            client_secret="GOCSPX-configured",
            **overrides,
        )

    def test_gmail_registers_one_and_the_fake_mailbox_does_not(self) -> None:
        """A folder of .eml files needs no browser; an OAuth mailbox does."""
        registry = composition.provider_registry()

        assert registry.needs_consent(MailProvider.GMAIL) is True
        assert registry.needs_consent(MailProvider.FAKE) is False

    async def test_it_runs_the_flow_with_the_configured_client(
        self, monkeypatch
    ) -> None:
        config = self._config()
        monkeypatch.setattr(composition, "google_config", lambda: config)
        seen: list[GmailConfig] = []
        granted = GmailCredentials(refresh_token="1//earned")

        async def fake_consent(
            passed: GmailConfig, *, login_hint: str | None = None
        ) -> GmailCredentials:
            seen.append(passed)
            return granted

        monkeypatch.setattr(composition, "run_consent_async", fake_consent)

        secret = await composition.gmail_consent({})

        assert seen == [config], "the flow reads the installation's own client"
        assert secret == granted.to_secret()

    async def test_the_accounts_address_becomes_the_login_hint(
        self, monkeypatch
    ) -> None:
        """So Google opens the consent on that account instead of a chooser."""
        monkeypatch.setattr(composition, "google_config", self._config)
        hints: list[str | None] = []

        async def fake_consent(
            passed: GmailConfig, *, login_hint: str | None = None
        ) -> GmailCredentials:
            hints.append(login_hint)
            return GmailCredentials(refresh_token="1//earned")

        monkeypatch.setattr(composition, "run_consent_async", fake_consent)

        await composition.gmail_consent({CONSENT_ADDRESS_KEY: "travel@example.com"})
        await composition.gmail_consent({})
        await composition.gmail_consent({CONSENT_ADDRESS_KEY: ""})

        assert hints == ["travel@example.com", None, None]

    async def test_it_asks_the_user_for_nothing(self, monkeypatch) -> None:
        """The values mapping is empty because the descriptor declares no fields.

        The argument stays in the signature because the seam is shared: IMAP's
        consent, when there is one, will have a host to read out of it.
        """
        monkeypatch.setattr(composition, "google_config", self._config)
        monkeypatch.setattr(
            composition,
            "run_consent_async",
            AsyncMock(return_value=GmailCredentials(refresh_token="1//earned")),
        )

        assert await composition.gmail_consent({}) is not None
        assert GmailSource.DESCRIPTOR.credential_fields == ()

    async def test_an_unconfigured_installation_says_so_instead_of_opening_a_browser(
        self, monkeypatch
    ) -> None:
        """Otherwise the window opens straight onto a Google error page."""
        monkeypatch.setattr(
            composition, "google_config", lambda: GmailConfig(client_id="")
        )
        opened = AsyncMock()
        monkeypatch.setattr(composition, "run_consent_async", opened)

        with pytest.raises(MailAuthError, match=re.escape("app.google.client_id")):
            await composition.gmail_consent({})

        opened.assert_not_awaited()


class TestTheM365Consent:
    """The second step for a provider that has two of them, one without a browser.

    Registered as one runner, because ``mailarc-ui`` asks the registry whether
    a provider has a second step and gets one answer per provider — and the
    answer for Microsoft 365 as a whole is yes. Which of the two paths runs is
    read off the credential fields the user filled in, inside the component
    that owns the credential model; this module only binds a configuration to
    it.

    Every test here asserts through the registered runner rather than through
    ``mailarc_m365`` directly, because the thing that can be wrong at this
    layer is the binding: a runner built from the wrong config, or from none.
    """

    APP_ONLY = {
        "mode": M365Mode.APP_ONLY.value,
        "tenant_id": "contoso.onmicrosoft.com",
        "mailbox": "team@contoso.com",
    }
    """What the account form sends for a tenant-wide grant. All strings."""

    @staticmethod
    def _runner_bound_to(monkeypatch, config: M365Config):
        monkeypatch.setattr(composition, "m365_config", lambda: config)
        runner = composition.provider_registry().consent_for(MailProvider.M365)
        assert runner is not None
        return runner

    async def test_app_only_needs_no_browser_at_all(self, monkeypatch) -> None:
        """The distinction this registration exists to be faithful about.

        A service principal was consented once, in the tenant, by an
        administrator — there is no per-account screen. So the runner returns a
        storable credential having opened nothing, and the account is connected
        the moment the form is submitted.
        """
        runner = self._runner_bound_to(
            monkeypatch,
            M365Config(client_id="a-client", client_secret="an-installation-secret"),
        )

        secret = await runner(self.APP_ONLY)

        stored = json.loads(secret)
        assert stored["mode"] == M365Mode.APP_ONLY.value
        assert stored["mailbox"] == "team@contoso.com"
        assert stored["tenant_id"] == "contoso.onmicrosoft.com"

    async def test_app_only_reads_this_installations_client_secret(
        self, monkeypatch
    ) -> None:
        """The one setting only the composition root can supply.

        A client-credentials grant presents the *installation's* secret, never
        one copied onto the account — two copies drift the day somebody
        rotates it. So an installation without one has to say so on the page
        holding the fields, rather than letting Entra answer ``AADSTS7000215``.
        """
        runner = self._runner_bound_to(monkeypatch, M365Config(client_id="a-client"))

        with pytest.raises(MailAuthError, match="app_m365_client_secret"):
            await runner(self.APP_ONLY)

    async def test_an_unconfigured_installation_says_so_instead_of_opening_a_browser(
        self, monkeypatch
    ) -> None:
        """The delegated half of the same question, and Gmail's test's sibling.

        No Entra application means the browser would open straight onto a
        Microsoft error page. Nothing is stubbed here: a runner that ignored
        the config it was bound to would reach ``webbrowser`` and hang the test
        out to its timeout rather than raise.
        """
        runner = self._runner_bound_to(monkeypatch, M365Config(client_id=""))

        with pytest.raises(MailAuthError, match="app_m365_client_id"):
            await runner({CONSENT_ADDRESS_KEY: "jens@contoso.com"})

    async def test_a_mode_nobody_recognises_is_refused_before_anything_opens(
        self, monkeypatch
    ) -> None:
        """``mode`` is a word a person types into a text box, so it can be junk.

        Refused rather than defaulted: somebody who typed something meant
        something by it, and silently signing them in as themselves when they
        asked for a service principal would archive the wrong mailbox under
        the right name.
        """
        runner = self._runner_bound_to(
            monkeypatch,
            M365Config(client_id="a-client", client_secret="an-installation-secret"),
        )

        with pytest.raises(MailAuthError, match="sign-in mode"):
            await runner({**self.APP_ONLY, "mode": "service-principal"})
