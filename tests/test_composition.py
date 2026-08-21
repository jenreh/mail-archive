"""The composition root: what the web application builds, and when."""

import functools
import importlib
import logging
import re
import sys
from unittest.mock import AsyncMock

import pytest
from appkit_commons.registry import service_registry

from app import composition
from app.configuration import configure
from mailarc_core import (
    ArchiveConfig,
    ArchiveReader,
    FalkorDBServer,
    GraphConfig,
    GraphServerMode,
)
from mailarc_core.mail.config import MailConfig
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.model import MailProvider
from mailarc_core.mail.ports import CONSENT_ADDRESS_KEY
from mailarc_google import GmailSource
from mailarc_google.source import GmailConfig, GmailCredentials
from mailarc_sync.engine import FakeMailSource, ProviderRegistry, SyncConfig
from mailarc_ui.accounts import provider_registry as registry_the_ui_sees
from mailarc_ui.review import archive_reader as reader_the_ui_sees

CONFIGS = (GraphConfig, SyncConfig, ArchiveConfig, MailConfig, GmailConfig)
"""Every configuration object the root hands out, and the getter for each."""

REGISTRY_LOGGER = "appkit_commons.registry"
"""Who says "overwriting" when a registration replaces one that was there."""

GMAIL_SECRET = GmailCredentials(refresh_token="refresh").to_secret()
"""A stored Gmail credential, in the shape the factory reads it back from.

A refresh token and nothing else: the OAuth client belongs to the installation
and is read from ``app.google``, not copied into every account row.
"""

SLEEPER = (sys.executable, "-c", "import time; time.sleep(30)")
"""A child that outlives the test unless it is stopped."""

DIES = (sys.executable, "-c", "raise SystemExit(3)")
"""A child that is gone before the start returns."""


def _getter(
    config: type[GraphConfig | SyncConfig | ArchiveConfig | MailConfig | GmailConfig],
):
    if config is GraphConfig:
        return composition.graph_config
    if config is SyncConfig:
        return composition.sync_config
    if config is ArchiveConfig:
        return composition.archive_config
    if config is MailConfig:
        return composition.mail_config
    return composition.google_config


@pytest.fixture(autouse=True)
def _clear_caches():
    """The composition root memoises; each test needs a clean slate."""
    composition.graph_server.cache_clear()
    composition.provider_registry.cache_clear()
    composition.archive_reader.cache_clear()
    composition.sync_worker.cache_clear()
    yield
    composition.graph_server.cache_clear()
    composition.provider_registry.cache_clear()
    composition.archive_reader.cache_clear()
    composition.sync_worker.cache_clear()


def _use_config(monkeypatch, mode: GraphServerMode) -> GraphConfig:
    config = GraphConfig(mode=mode, host="127.0.0.1", port=6379)
    monkeypatch.setattr(composition, "graph_config", lambda: config)
    return config


def test_the_server_is_built_from_the_registered_configuration(monkeypatch) -> None:
    config = _use_config(monkeypatch, GraphServerMode.REMOTE)

    server = composition.graph_server()

    assert isinstance(server, FalkorDBServer)
    assert server.endpoint == config.endpoint


def test_the_server_is_a_singleton(monkeypatch) -> None:
    """One server per process — one per caller would leak a redis-server."""
    _use_config(monkeypatch, GraphServerMode.LOCAL)

    assert composition.graph_server() is composition.graph_server()


@pytest.mark.parametrize("config", CONFIGS, ids=lambda one: one.__name__)
def test_a_config_comes_from_the_registry(config) -> None:
    registry = service_registry()
    saved = registry.snapshot()
    registered = config()
    registry.register_as(config, registered)
    try:
        assert _getter(config)() is registered
    finally:
        registry.restore(saved)


@pytest.mark.parametrize("config", CONFIGS, ids=lambda one: one.__name__)
def test_configuring_the_application_registers_the_config(config) -> None:
    """The getters can only find what ``AppConfig`` actually carries."""
    configure()  # cached; importing `app` already ran it

    assert isinstance(_getter(config)(), config)


@pytest.mark.parametrize("config", CONFIGS, ids=lambda one: one.__name__)
def test_a_config_explains_itself_when_unregistered(config, monkeypatch) -> None:
    monkeypatch.setattr(composition.service_registry(), "get", lambda _: None)

    with pytest.raises(RuntimeError, match=f"{config.__name__}.*configure"):
        _getter(config)()


def test_the_startup_error_comes_from_the_server(monkeypatch) -> None:
    _use_config(monkeypatch, GraphServerMode.REMOTE)
    composition.graph_server()._startup_error = "run `task tauri:vendor`"

    assert composition.graph_startup_error() == "run `task tauri:vendor`"


async def test_graph_status_reads_the_configured_server(monkeypatch) -> None:
    config = _use_config(monkeypatch, GraphServerMode.REMOTE)
    seen: list[GraphConfig] = []

    async def fake_read(cfg: GraphConfig) -> str:
        seen.append(cfg)
        return "status"

    monkeypatch.setattr(composition, "read_status_async", fake_read)

    assert await composition.graph_status() == "status"
    assert seen == [config]


class TestLifespan:
    @staticmethod
    def _recording_server(monkeypatch) -> list[str]:
        events: list[str] = []

        class Recording:
            async def start_async(self) -> None:
                events.append("start")

            async def stop_async(self) -> None:
                events.append("stop")

        recorder = Recording()
        monkeypatch.setattr(composition, "graph_server", lambda: recorder)
        return events

    async def test_starts_on_entry_and_stops_on_exit(self, monkeypatch) -> None:
        events = self._recording_server(monkeypatch)

        async with composition.graph_server_lifespan():
            assert events == ["start"]

        assert events == ["start", "stop"]

    async def test_a_failed_start_does_not_take_the_app_down(self, monkeypatch) -> None:
        """The page whose job is reporting server state is more useful up."""
        events: list[str] = []

        class Broken:
            async def start_async(self) -> None:
                raise RuntimeError("run `task tauri:vendor`")

            async def stop_async(self) -> None:
                events.append("stop")

        broken = Broken()
        monkeypatch.setattr(composition, "graph_server", lambda: broken)

        async with composition.graph_server_lifespan():
            pass

        assert events == ["stop"]

    async def test_stops_even_when_the_app_body_raises(self, monkeypatch) -> None:
        events = self._recording_server(monkeypatch)

        with pytest.raises(ValueError):
            async with composition.graph_server_lifespan():
                raise ValueError("app blew up")

        assert events == ["start", "stop"]


def test_the_registry_offers_every_provider_this_build_ships() -> None:
    """Both of them, in registration order — that is the order the account
    form lists, so the first one is what a new user is offered."""
    registry = composition.provider_registry()

    assert [one.provider for one in registry.descriptors()] == [
        MailProvider.FAKE,
        MailProvider.GMAIL,
    ]


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


def test_the_reader_is_built_on_the_configured_stores(monkeypatch, tmp_path) -> None:
    """The review page must list what the worker wrote: same graph, same blob
    store. Read off the reader's own parts, because that is what would differ
    if a wire pointed elsewhere."""
    graph = _use_config(monkeypatch, GraphServerMode.REMOTE)
    archive = ArchiveConfig(store_dir=tmp_path / "blobs")
    monkeypatch.setattr(composition, "archive_config", lambda: archive)

    reader = composition.archive_reader()

    assert reader is composition.archive_reader()
    assert reader._blobs.root == archive.store_dir
    assert isinstance(reader._graph_session, functools.partial)
    assert reader._graph_session.args == (graph,)


@pytest.mark.usefixtures("_published_registry")
def test_the_ui_finds_the_reader_without_importing_the_app() -> None:
    """Same hand-over as the provider registry, asserted through the UI's own
    lookup because that is the code a broken one would break."""
    published = composition.publish_archive_reader()

    assert published is composition.archive_reader()
    assert reader_the_ui_sees() is published


@pytest.mark.usefixtures("_published_registry")
def test_publishing_the_reader_twice_leaves_one(caplog) -> None:
    first = composition.publish_archive_reader()

    with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
        assert composition.publish_archive_reader() is first

    assert service_registry().get(ArchiveReader) is first
    assert caplog.records == []


def test_the_worker_handle_is_a_singleton() -> None:
    """A second handle would be a second child claiming the same jobs."""
    assert composition.sync_worker() is composition.sync_worker()


class TestWorkerProcess:
    """The child itself, against real processes: a fake proves nothing here."""

    async def test_starts_a_child_and_takes_it_down_again(self) -> None:
        """Through the async pair, because that is what the lifespan calls."""
        worker = composition.WorkerProcess(SLEEPER)

        await worker.start_async()
        child = worker._process
        assert worker.running
        assert worker.startup_error is None

        await worker.stop_async()

        assert not worker.running
        assert child is not None
        assert child.poll() is not None, "the child outlived the stop"

    def test_a_child_that_dies_on_the_spot_is_a_failed_start(self) -> None:
        """Nothing listens on a port here, so exiting is the only symptom."""
        worker = composition.WorkerProcess(DIES)

        with pytest.raises(RuntimeError, match="code 3"):
            worker.start()

        assert worker.startup_error is not None
        assert not worker.running

    def test_stopping_one_that_never_started_is_allowed(self) -> None:
        """A lifespan whose start failed still runs its finally."""
        composition.WorkerProcess(SLEEPER).stop()

    def test_the_command_names_a_module_that_can_be_run(self) -> None:
        """`task sync:worker` runs the same module by hand — a typo here would
        only show up as a child that dies at every application start."""
        module = importlib.import_module(composition.WORKER_COMMAND[-1])

        assert callable(module.main)


class TestSyncWorkerLifespan:
    @staticmethod
    def _supervised(monkeypatch, supervise: bool = True) -> list[str]:
        monkeypatch.setattr(
            composition, "sync_config", lambda: SyncConfig(supervise_worker=supervise)
        )
        events: list[str] = []

        class Recording:
            async def start_async(self) -> None:
                events.append("start")

            async def stop_async(self) -> None:
                events.append("stop")

        recorder = Recording()
        monkeypatch.setattr(composition, "sync_worker", lambda: recorder)
        return events

    async def test_starts_on_entry_and_stops_on_exit(self, monkeypatch) -> None:
        events = self._supervised(monkeypatch)

        async with composition.sync_worker_lifespan():
            assert events == ["start"]

        assert events == ["start", "stop"]

    async def test_a_failed_start_does_not_take_the_app_down(self, monkeypatch) -> None:
        """A job simply waits in the queue; the archive is readable meanwhile."""
        monkeypatch.setattr(composition, "sync_config", SyncConfig)
        events: list[str] = []

        class Broken:
            async def start_async(self) -> None:
                raise RuntimeError("the sync worker exited immediately")

            async def stop_async(self) -> None:
                events.append("stop")

        broken = Broken()
        monkeypatch.setattr(composition, "sync_worker", lambda: broken)

        async with composition.sync_worker_lifespan():
            pass

        assert events == ["stop"]

    async def test_stops_even_when_the_app_body_raises(self, monkeypatch) -> None:
        events = self._supervised(monkeypatch)

        with pytest.raises(ValueError):
            async with composition.sync_worker_lifespan():
                raise ValueError("app blew up")

        assert events == ["start", "stop"]

    async def test_supervision_can_be_handed_to_docker(self, monkeypatch) -> None:
        """Off under systemd: a second copy would claim the same jobs."""
        events = self._supervised(monkeypatch, supervise=False)

        async with composition.sync_worker_lifespan():
            pass

        assert events == []


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
