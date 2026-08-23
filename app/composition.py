"""Composition root: what the application builds, and who owns it afterwards.

The only module in the web application that builds a component from
configuration. Everything else asks this module and never constructs a server,
a connection or a provider itself — which is also why this is the one file that
is allowed to name an implementation (§4.1).
"""

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from functools import lru_cache, partial
from typing import Any

from appkit_commons.database.session import get_asyncdb_session
from appkit_commons.registry import service_registry
from pydantic import ValidationError

from mailarc_analytics import AnalyticsConfig, AnalyticsReader
from mailarc_analytics.semantic import (
    EmbedderPort,
    SemanticConfig,
    SemanticControl,
    SemanticOverrides,
    SemanticSearch,
    build_embedder,
)
from mailarc_core import (
    ArchiveConfig,
    ArchiveReader,
    BlobStore,
    FalkorDBServer,
    GraphConfig,
    GraphServerStatus,
    read_status_async,
)
from mailarc_core.database.repositories import SemanticSettingsRepository
from mailarc_core.graph.client import session as graph_session
from mailarc_core.mail.config import MailConfig
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.ports import CONSENT_ADDRESS_KEY
from mailarc_google import GmailSource
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.oauth import run_consent_async
from mailarc_sync.engine import (
    FAKE_DESCRIPTOR,
    FakeMailSource,
    ProviderRegistry,
    SyncConfig,
)
from mailarc_sync.jobs import SessionFactory

logger = logging.getLogger(__name__)

_SEMANTIC_SETTINGS = SemanticSettingsRepository()
"""The one row a human's embedder choice is stored in."""

_semantic_override: SemanticConfig | None = None
"""The merge of the stored settings over the file, once it has been read.

``None`` until :func:`load_semantic_config` has run, and that is not a
degraded state: it is the configuration this application had before any of
this existed, and the one a process that never reaches a database keeps.
"""


def _registered[T](config: type[T]) -> T:
    """One configuration object, or the sentence that says what is missing.

    ``ServiceRegistry.get`` raises ``KeyError``; it never returns ``None``, so
    the ``is None`` branch this used to carry could not run and the explanation
    written for it never reached anybody. An un-configured process got a bare
    ``KeyError: 'Instance of type GraphConfig not found in registry'`` instead.
    Same shape as :func:`mailarc_ui.insights.state.analytics_reader`, which had
    it right.
    """
    try:
        return service_registry().get(config)
    except KeyError as error:
        raise RuntimeError(
            f"{config.__name__} is not registered — "
            "was app.configuration.configure() called?"
        ) from error


def graph_config() -> GraphConfig:
    return _registered(GraphConfig)


def sync_config() -> SyncConfig:
    return _registered(SyncConfig)


def archive_config() -> ArchiveConfig:
    return _registered(ArchiveConfig)


def analytics_config() -> AnalyticsConfig:
    return _registered(AnalyticsConfig)


def semantic_config() -> SemanticConfig:
    """The embedder this installation is actually configured with.

    The file and the environment, with whatever a human stored in
    ``semantic_settings`` laid over it — and, until
    :func:`load_semantic_config` has run, exactly the file and the environment.
    That ordering is the guarantee rather than a fallback: a process that never
    opens the database, an installation whose table is empty, and a checkout
    whose migration has not been applied yet all resolve to the same
    configuration this function returned before the settings row existed, which
    on a default installation is ``provider: none``.

    Deliberately still a plain synchronous read. Three callers reach it —
    :func:`semantic_embedder`, :func:`semantic_search` and
    :func:`app.derive.rebuild`, the last from inside a worker thread — and
    making it ``async`` to hide a database read would put an ``await`` in a
    thread that has no loop. The read happens once, in
    :func:`load_semantic_config`, where there is one.
    """
    if _semantic_override is not None:
        return _semantic_override
    return _registered(SemanticConfig)


async def load_semantic_config(
    session_factory: SessionFactory = get_asyncdb_session,
) -> SemanticConfig:
    """Read the stored embedder settings and adopt the merge as effective.

    The merge itself is one line — stored over configured, unset falls through
    — and it lives here because §4.1 puts building a component from
    configuration in this module alone: ``mailarc-analytics`` describes what an
    embedder is and must not learn that a database holds one setting of it.

    Idempotent, and cheap when nothing changed: a settings row that resolves to
    the configuration already in force returns without clearing a cache,
    without closing a client and without re-registering anything. That is what
    makes "a fresh installation behaves exactly as today" a property of the
    code rather than a claim about it — with no row stored,
    :meth:`SemanticOverrides.applied_to` hands back the configured object
    itself and this function is a no-op.

    When something *did* change, the two cached objects built from the old
    configuration are dropped and the old embedder is closed — it holds an
    ``httpx`` pool, and a cache cleared without an ``aclose`` leaks a
    connection per save. The search is then published again so the page that
    reads it out of the service registry sees the new one; ``mailarc-ui`` may
    not import ``app``, so a stale object there would be a stale object
    forever.
    """
    merged = (await _stored_semantic_overrides(session_factory)).applied_to(
        _registered(SemanticConfig)
    )
    if merged == semantic_config():
        logger.debug("The stored embedder settings change nothing")
        return merged
    await _adopt_semantic_config(merged)
    # All three identifying values, not just the provider: a partial merge (see
    # `_validated_overrides`) resolves to neither the file nor the row, so this
    # is the only place "which embedder is this process actually running" can
    # be answered afterwards. Never the key — nothing in this module logs it.
    logger.info(
        "Embedder settings adopted: provider=%s model=%s dimension=%d",
        merged.provider,
        merged.model or "<the provider's default>",
        merged.dimension,
    )
    return merged


async def _stored_semantic_overrides(
    session_factory: SessionFactory,
) -> SemanticOverrides:
    """The stored row as overrides, or nothing at all."""
    async with session_factory() as session:
        stored = await _SEMANTIC_SETTINGS.load(session)
    if stored is None:
        return SemanticOverrides()
    return _validated_overrides(
        {
            "provider": stored.provider,
            "model": stored.model,
            "dimension": stored.dimension,
            "base_url": stored.base_url,
            "api_key": stored.api_key,
        }
    )


def _validated_overrides(stored: dict[str, Any]) -> SemanticOverrides:
    """The stored columns that validate, without the ones that do not.

    A row that does not validate — a provider name no release of this
    application ever wrote, a dimension a hand edit set to zero — must not stop
    the archive starting. But dropping the *whole row* over one bad column was
    too blunt: a good provider, a good model and a hand-edited ``dimension`` of
    zero left the archive running the configuration file's embedder, which on a
    default installation is ``provider: none``, while the settings page went on
    redisplaying the stored provider. Four decisions lost to one bad value, and
    the only trace a single warning naming ``dimension``.

    So each refusal costs its own field and the rest are kept. The offenders
    are found by asking pydantic which fields it blamed and building the model
    again without them, rather than by validating field by field — the model is
    the authority on what is valid, and a second implementation of that here
    would be a second answer to drift from it.

    Only the *names* are logged, never pydantic's own message:
    ``ValidationError`` quotes the input that failed, and one of these five
    fields is an API key. A refusal that names no field at all — what a model
    validator produces — cannot be narrowed to a column, so the whole row goes;
    that branch also exists because indexing into an empty ``loc`` used to
    raise ``IndexError`` out of the handler and chain the ``ValidationError``,
    quoted inputs and all, into somebody's ``logger.exception``.
    """
    candidates = dict(stored)
    refused: list[str] = []
    while True:
        try:
            overrides = SemanticOverrides(**candidates)
        except ValidationError as error:
            named = [name for name in _refused_fields(error) if name in candidates]
            if not named:
                logger.warning(
                    "Ignoring every stored embedder setting: the row does not "
                    "validate and no single field was blamed. The "
                    "configuration file answers for all of them."
                )
                return SemanticOverrides()
            refused.extend(named)
            for name in named:
                del candidates[name]
            continue
        break
    if refused:
        logger.warning(
            "Ignoring these stored embedder settings; these do not validate: "
            "%s. The configuration file answers for them.",
            ", ".join(sorted(refused)),
        )
    return overrides


def _refused_fields(error: ValidationError) -> list[str]:
    """Which fields pydantic blamed — the names alone, never the values.

    Joined over the whole ``loc`` rather than indexed at ``[0]``, which cannot
    run off the end of an empty one. An entry with no ``loc`` is left out
    rather than named ``<model>``: the caller's question is "which key do I
    remove", and a root-level refusal has no answer to it.
    """
    return [
        ".".join(str(part) for part in one["loc"])
        for one in error.errors()
        if one["loc"]
    ]


async def _adopt_semantic_config(merged: SemanticConfig) -> None:
    """Make *merged* the effective configuration and rebuild what came before."""
    global _semantic_override
    # Read before the swap, and only if one was ever built: asking for the
    # embedder here would otherwise construct one from the configuration being
    # replaced, just to close it again.
    stale = semantic_embedder() if semantic_embedder.cache_info().currsize else None
    _semantic_override = merged
    semantic_embedder.cache_clear()
    semantic_search.cache_clear()
    if stale is not None:
        await stale.aclose()
    if service_registry().has(SemanticSearch):
        publish_semantic_search()


async def adopt_semantic_settings() -> None:
    """Adopt the stored embedder settings for this process, whatever happens.

    Every entry point that reads or writes a vector has to run this, and each
    of the four has its own composition root: the web application, the import
    worker, ``python -m app.embedding`` and ``python -m app.derive`` are four
    processes and :data:`_semantic_override` starts out ``None`` in all of
    them. A command that skipped it would embed with the *file's* embedder
    while the pages searched with the stored one — and that failure is silent
    at both ends, because ``SEMANTIC_NEIGHBOURS`` filters on
    ``embedding_model`` and simply returns fewer rows. So this exists as one
    named step the next entry point can be pointed at, rather than as four
    copies of two lines, one of which will be forgotten.

    A failure is logged and swallowed, and that is policy rather than
    sloppiness. The two real ones are a database whose migration has not been
    applied, so ``semantic_settings`` does not exist yet, and a stored row
    somebody edited by hand; neither is a reason to refuse to start or to
    refuse to embed. Both leave :func:`semantic_config` answering with the file
    and the environment, which is a complete state (§7.4) and not a broken one.
    """
    try:
        await load_semantic_config()
    except Exception:
        logger.exception("Could not read the stored embedder settings")


@contextlib.asynccontextmanager
async def semantic_settings_lifespan() -> AsyncIterator[None]:
    """ASGI lifespan hook: adopt the stored embedder settings before serving.

    Policy only, and the same policy the other two hooks get — see
    :func:`adopt_semantic_settings`, which is where the swallow lives so that
    the worker and the two commands get exactly this one.

    At startup rather than at import, because ``app/app.py`` publishes the
    search while it is being imported and there is no event loop yet to read a
    database with. A lifespan runs before the first request, so nothing a
    person can reach ever sees the un-merged configuration.
    """
    await adopt_semantic_settings()
    yield


def mail_config() -> MailConfig:
    return _registered(MailConfig)


def google_config() -> GmailConfig:
    return _registered(GmailConfig)


@lru_cache(maxsize=1)
def graph_server() -> FalkorDBServer:
    """The application-wide graph server handle.

    Cached deliberately: a local server is a real child process, and one per
    caller would leak a redis-server for every request.
    """
    return FalkorDBServer(graph_config())


async def graph_status() -> GraphServerStatus:
    """Read a fresh snapshot of the graph server."""
    return await read_status_async(graph_config())


def graph_startup_error() -> str | None:
    """Why the graph server failed to start, if it did."""
    return graph_server().startup_error


@contextlib.asynccontextmanager
async def graph_server_lifespan() -> AsyncIterator[None]:
    """ASGI lifespan hook: own the graph server for as long as the app runs.

    Policy only: the core knows *how* to start and stop without blocking the
    loop, this decides what a failure means. A failed start is logged and
    swallowed rather than killing the app — the page whose whole job is
    reporting server state is more useful up than down, and it shows the
    reason via :func:`graph_startup_error`.
    """
    server = graph_server()
    try:
        await server.start_async()
    except Exception:
        logger.exception("Could not start the graph server")
    try:
        yield
    finally:
        await server.stop_async()


@lru_cache(maxsize=1)
def provider_registry() -> ProviderRegistry:
    """Every mailbox kind this installation can actually open.

    The registry is a singleton because it is a list of decisions, not state:
    building a second one would give two answers to "which providers exist".
    """
    registry = ProviderRegistry()
    registry.register(FAKE_DESCRIPTOR, FakeMailSource.create)
    registry.register(
        GmailSource.DESCRIPTOR,
        GmailSource.using(google_config()),
        consent=gmail_consent,
    )
    return registry


async def gmail_consent(values: Mapping[str, str]) -> str:
    """Walk the user through Google's consent screen and return what to store.

    The :data:`~mailarc_core.mail.ports.ConsentRunner` for Gmail, and the whole
    reason that alias exists: opening a browser is not something a mailbox can
    be asked to do through the port, and ``mailarc-ui`` may not import a
    provider to reach one (§4.1). So the browser half is registered here, in
    the one module allowed to name Gmail, and the account page only knows that
    this provider has a second step.

    ``values`` are whatever the descriptor asked the user for, and Gmail's
    asks for nothing: the OAuth client is this installation's, configured once
    under ``app.google``, so a person adding a mailbox types an address and
    presses Connect. What the mapping does carry is that address, under
    :data:`~mailarc_core.mail.ports.CONSENT_ADDRESS_KEY`, and it goes to Google
    as the ``login_hint`` so the consent screen opens on the right account.
    """
    config = google_config()
    if not config.configured():
        raise MailAuthError(
            "Gmail is not set up on this installation — set app.google.client_id "
            "and app.google.client_secret in the configuration"
        )
    credentials = await run_consent_async(
        config, login_hint=values.get(CONSENT_ADDRESS_KEY) or None
    )
    return credentials.to_secret()


@lru_cache(maxsize=1)
def archive_reader() -> ArchiveReader:
    """The read side of the archive, wired to this installation's stores.

    The same pair the worker writes with — the configured graph and the blob
    store under ``archive.store_dir`` — so what the review page lists is what
    the import wrote. Cached like the other handles: it holds no connection,
    but it is one decision and two objects would be two answers to "where is
    the archive".
    """
    return ArchiveReader(
        graph_session=partial(graph_session, graph_config()),
        blobs=BlobStore(archive_config()),
    )


def publish_archive_reader() -> ArchiveReader:
    """Leave the reader where the review page can find it.

    Same route and same reason as :func:`publish_provider_registry`:
    ``mailarc-ui`` may not import ``app``, and this is the one module allowed
    to build a component from configuration. Saying it twice is a no-op.
    """
    services = service_registry()
    reader = archive_reader()
    if services.has(ArchiveReader) and services.get(ArchiveReader) is reader:
        return reader
    services.register_as(ArchiveReader, reader)
    return reader


@lru_cache(maxsize=1)
def analytics_reader() -> AnalyticsReader:
    """The read side of the derived layer, on the graph the rebuild wrote to.

    One session factory and no second store, unlike :func:`archive_reader`:
    everything a rebuild leaves behind is a node or an edge, so there is no
    blob half to wire. The graph is deliberately the one the archive reader
    reads as well — the insights page holds a derived edge against the messages
    it was derived from, and two different graphs would make that comparison
    meaningless rather than merely wrong. Cached for the reason the archive
    reader is: one decision, and two objects would be two answers to "which
    graph is being analysed".
    """
    return AnalyticsReader(graph_session=partial(graph_session, graph_config()))


def publish_analytics_reader() -> AnalyticsReader:
    """Leave the reader where the insights page can find it.

    Same route and same reason as :func:`publish_archive_reader`, down to the
    second call being a no-op: ``mailarc-ui`` may not import ``app``, and this
    is the one module allowed to build a component from configuration.
    """
    services = service_registry()
    reader = analytics_reader()
    if services.has(AnalyticsReader) and services.get(AnalyticsReader) is reader:
        return reader
    services.register_as(AnalyticsReader, reader)
    return reader


@lru_cache(maxsize=1)
def semantic_embedder() -> EmbedderPort | None:
    """This installation's embedder, or ``None`` because none is configured.

    The only place in the running application that turns a
    :class:`~mailarc_analytics.semantic.config.SemanticConfig` into a client —
    so "which model is this archive embedded with" has exactly one answer per
    process, and the embed job and the search cannot disagree about it.

    ``None`` is the default and a complete state rather than a broken one
    (§7.4): everything deterministic keeps working and the two surfaces that
    need a vector say so in a sentence naming the setting. Cached because the
    adapters hold an ``httpx`` connection pool — a second one would pay a fresh
    handshake per query and never be closed.
    """
    return build_embedder(semantic_config())


@lru_cache(maxsize=1)
def semantic_search() -> SemanticSearch:
    """Both search paths, on the graph the import wrote to.

    The same graph as :func:`archive_reader` and :func:`analytics_reader`, for
    the same reason: a search result is a list of messages the review page is
    expected to be able to open, and a second graph would make that a broken
    link rather than a visible mistake.

    Built even when there is no embedder. The full-text half needs none — it
    reads the index the baseline migration created — and it is exactly the half
    that has to keep working on a default installation, so an application that
    only published a search when one was configured would take the working path
    down with the unconfigured one.
    """
    return SemanticSearch(
        graph_session=partial(graph_session, graph_config()),
        config=semantic_config(),
        embedder=semantic_embedder(),
    )


def publish_semantic_search() -> SemanticSearch:
    """Leave the search where the insights page can find it.

    Same route and same reason as :func:`publish_analytics_reader`:
    ``mailarc-ui`` may not import ``app``, and building an embedder from
    configuration is this module's alone. Without this call the panel meets its
    own developer error — both paths, full text included.
    """
    services = service_registry()
    search = semantic_search()
    if services.has(SemanticSearch) and services.get(SemanticSearch) is search:
        return search
    services.register_as(SemanticSearch, search)
    return search


async def _reindex() -> int:
    """Rebuild the vector index at the configured length. See :mod:`app.reindex`."""
    from app.reindex import reindex

    return await reindex()


def publish_semantic_control() -> SemanticControl:
    """Leave the embedder's two verbs where the settings page can find them.

    The settings form is the one page that changes what
    :func:`semantic_config` answers, so it needs to read that answer back and
    to make a save take effect — and both are this module's work: reading the
    stored row and rebuilding the embedder from it is building a component from
    configuration, which §4.1 leaves here alone.

    Registered rather than imported, for the reason everything else on this
    page is: ``mailarc-ui`` may not import ``app``. What goes in is the two
    functions themselves and not their results — the configuration in force
    changes every time somebody saves, and a registry entry holding the object
    it was handed at startup would report the embedder it replaced.

    Registered once and never replaced: the entry is two references to
    module-level functions, so a second call has nothing new to say and
    overwriting it would only log noise.
    """
    services = service_registry()
    if services.has(SemanticControl):
        return services.get(SemanticControl)
    control = SemanticControl(
        current=semantic_config,
        reload=load_semantic_config,
        # Imported here rather than at module scope: `app.reindex` imports this
        # module for the graph configuration, and naming it at the top would
        # close that circle at import time.
        reindex=_reindex,
    )
    services.register_as(SemanticControl, control)
    return control


def publish_provider_registry() -> ProviderRegistry:
    """Leave the provider list where the browser half can find it.

    ``mailarc-ui`` is a component and may not import ``app`` (§4.1), so it
    reads the registry out of the service registry — the same route every
    configuration takes, and the reason this module stays the only one that
    names a provider.

    Saying it twice is a no-op rather than an overwrite: the application is
    reloadable, and re-registering the same object would only log noise.
    """
    services = service_registry()
    registry = provider_registry()
    if services.has(ProviderRegistry) and services.get(ProviderRegistry) is registry:
        return registry
    services.register_as(ProviderRegistry, registry)
    return registry


WORKER_COMMAND = (sys.executable, "-m", "app.worker")
"""How the worker is started: a fresh interpreter, not a thread of this one.

An import runs for hours and holds a graph session; sharing an event loop with
the web application would make every page wait on it. ``-m app.worker`` rather
than an import, because that module is the worker's own entry point and its
process has no business holding Reflex.
"""

STARTUP_GRACE_SECONDS = 0.5
"""How long a start waits to see whether the child dies on the spot.

A worker that cannot import its dependencies is gone in milliseconds, and a
start that never looks would leave the application silently jobless.
"""

TERMINATE_GRACE_SECONDS = 10.0
"""Between SIGTERM and SIGKILL. The worker puts its job down on SIGTERM."""


class WorkerProcess:
    """The sync worker as a child of the application that started it.

    Same shape as :class:`~mailarc_core.graph.server.FalkorDBServer`, for the
    same reason: idempotent start and stop so a lifespan may fire twice, and a
    startup failure kept as text instead of thrown away, so the caller decides
    what a missing worker means.

    ``command`` is a parameter so a test can drive these mechanics against a
    child it controls; nothing in the application passes it.
    """

    def __init__(self, command: Sequence[str] = WORKER_COMMAND) -> None:
        self._command = tuple(command)
        self._process: subprocess.Popen[bytes] | None = None
        self._startup_error: str | None = None

    @property
    def running(self) -> bool:
        """Whether the child is alive right now."""
        return self._process is not None and self._process.poll() is None

    @property
    def startup_error(self) -> str | None:
        """Why the last start failed, if it did."""
        return self._startup_error

    def start(self) -> None:
        """Start the worker unless it is already running."""
        if self.running:
            logger.debug("The sync worker is already running")
            return
        try:
            self._spawn()
        except Exception as exc:
            self._startup_error = str(exc)
            raise
        else:
            self._startup_error = None

    def stop(self) -> None:
        """Ask the worker to finish, and insist if it will not."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return

        logger.info("Stopping the sync worker (pid %d)", process.pid)
        self._signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("The sync worker ignored SIGTERM, sending SIGKILL")
            self._signal_group(process, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=TERMINATE_GRACE_SECONDS)

    async def start_async(self) -> None:
        """:meth:`start` off the event loop; starting a process blocks."""
        await asyncio.to_thread(self.start)

    async def stop_async(self) -> None:
        """:meth:`stop` off the event loop; reaping a process blocks."""
        await asyncio.to_thread(self.stop)

    def _spawn(self) -> None:
        logger.info("Starting the sync worker: %s", " ".join(self._command))
        # The command is this interpreter plus a module name from our own
        # source; no user input reaches it. `start_new_session` gives the
        # worker its own process group, so `stop` takes down whatever it
        # started along with it.
        process = subprocess.Popen(self._command, start_new_session=True)  # noqa: S603
        self._process = process
        try:
            process.wait(timeout=STARTUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return
        raise RuntimeError(
            f"the sync worker exited immediately with code {process.returncode}"
        )

    @staticmethod
    def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), sig)


@lru_cache(maxsize=1)
def sync_worker() -> WorkerProcess:
    """The one worker child this application supervises.

    Cached for the reason :func:`graph_server` is: a second handle would be a
    second child claiming the same jobs.
    """
    return WorkerProcess()


@contextlib.asynccontextmanager
async def sync_worker_lifespan() -> AsyncIterator[None]:
    """ASGI lifespan hook: own the sync worker for as long as the app runs.

    Policy only, and the same policy the graph server gets: a failed start is
    logged and swallowed rather than killing the application — the pages that
    show what the archive holds keep working without a worker, and a job simply
    waits in the queue until one exists.

    ``sync.supervise_worker`` turns this off where somebody else already runs
    the worker: under Docker or systemd it is its own unit, and a second copy
    started here would claim the same jobs.
    """
    if not sync_config().supervise_worker:
        logger.info("Not supervising the sync worker — something else owns it")
        yield
        return

    worker = sync_worker()
    try:
        await worker.start_async()
    except Exception:
        logger.exception("Could not start the sync worker")
    try:
        yield
    finally:
        await worker.stop_async()
