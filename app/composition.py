"""Composition root: what the application builds, and who owns it afterwards.

The only module in the web application that builds a component from
configuration. Everything else asks this module and never constructs a server,
a connection or a provider itself — which is also why this is the one file that
is allowed to name an implementation (§4.1).
"""

import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from functools import lru_cache, partial
from pathlib import Path
from typing import Any

from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.session import get_asyncdb_session
from appkit_commons.registry import service_registry
from pydantic import BaseModel, ValidationError

# The two child processes this application owns live in `app.supervisors`: this
# module was over §1.6's thousand lines, and a process supervisor is not a
# component built from configuration. Re-exported rather than moved out of
# reach — `app/app.py` registers the lifespan and the status page reads the
# health, and both have always found them here. `F401` is suppressed for that:
# an import that exists only to be re-exported is the one case ruff cannot tell
# from an oversight.
from app.supervisors import (  # noqa: F401 — a deliberate re-export, see above
    STARTUP_GRACE_SECONDS,
    TERMINATE_GRACE_SECONDS,
    WORKER_COMMAND,
    WorkerProcess,
    graph_health,
    graph_server,
    graph_startup_error,
    graph_status,
    publish_graph_health,
    supervised_children_lifespan,
    sync_worker,
)
from mailarc_analytics import AnalyticsConfig, AnalyticsReader, GraphReader
from mailarc_analytics.nlq import (
    ChatPort,
    LlmConfig,
    LlmControl,
    LlmOverrides,
    QueryInterpreter,
    build_chat_client,
)
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
    GraphConfig,
    TagStore,
)
from mailarc_core.database import database_path
from mailarc_core.database.repositories import (
    LlmSettingsRepository,
    SemanticSettingsRepository,
)
from mailarc_core.graph.client import session as graph_session
from mailarc_core.mail.config import MailConfig
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.ports import CONSENT_ADDRESS_KEY
from mailarc_core.storage import StorageReader
from mailarc_google import GmailSource
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.oauth import run_consent_async
from mailarc_imap import ImapSource
from mailarc_imap.source.config import ImapConfig
from mailarc_m365 import M365Source, consent_runner
from mailarc_m365.source.config import M365Config
from mailarc_sync.engine import (
    FAKE_DESCRIPTOR,
    FakeMailSource,
    ProviderRegistry,
    SyncConfig,
)
from mailarc_sync.erase import AccountEraser
from mailarc_sync.jobs import SessionFactory

logger = logging.getLogger(__name__)

_SEMANTIC_SETTINGS = SemanticSettingsRepository()
"""The one row a human's embedder choice is stored in."""

_LLM_SETTINGS = LlmSettingsRepository()
"""The one row a human's interpreter choice is stored in.

Its own table rather than four more columns on the embedder's, because the two
share nothing but a shape — including ``updated``, which is each form's
concurrency baseline, so one save would otherwise invalidate the other's.
"""

_semantic_override: SemanticConfig | None = None
"""The merge of the stored settings over the file, once it has been read.

``None`` until :func:`load_semantic_config` has run, and that is not a
degraded state: it is the configuration this application had before any of
this existed, and the one a process that never reaches a database keeps.
"""

_llm_override: LlmConfig | None = None
"""The same, for the interpreter, and a separate variable on purpose.

One shared override would make adopting either model adopt whatever the other
was last left at — including ``None``, which is how a process that read only
one of the two tables would silently put the other back to the file.
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


def _validated[OverridesT: BaseModel](
    kind: type[OverridesT], stored: dict[str, Any], what: str
) -> OverridesT:
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
    would be a second answer to drift from it. The loop repeats because one
    pass is not enough: removing a blamed field can let the model get far
    enough to blame the next one, and a single retry would throw away every
    remaining good column the moment that happened.

    Only the *names* are logged, never pydantic's own message:
    ``ValidationError`` quotes the input that failed, and one of these fields
    is an API key. That is also why no branch here calls ``logger.exception``.
    A refusal that names no field at all — what a model validator produces —
    cannot be narrowed to a column, so the whole row goes; that branch also
    exists because indexing into an empty ``loc`` used to raise ``IndexError``
    out of the handler and chain the ``ValidationError``, quoted inputs and
    all, into somebody's ``logger.exception``.

    Generic over the model because there are two of these rows now, and the
    interpreter's copy had already drifted: it retried once, then discarded the
    row, and logged the ``ValidationError`` itself — putting a stored
    ``api_key`` into the log this docstring exists to keep it out of.
    """
    candidates = dict(stored)
    refused: list[str] = []
    while True:
        try:
            overrides = kind(**candidates)
        except ValidationError as error:
            named = [name for name in _refused_fields(error) if name in candidates]
            if not named:
                logger.warning(
                    "Ignoring every stored %s setting: the row does not "
                    "validate and no single field was blamed. The "
                    "configuration file answers for all of them.",
                    what,
                )
                return kind()
            refused.extend(named)
            for name in named:
                del candidates[name]
            continue
        break
    if refused:
        logger.warning(
            "Ignoring these stored %s settings; these do not validate: %s. "
            "The configuration file answers for them.",
            what,
            ", ".join(sorted(refused)),
        )
    return overrides


def _validated_overrides(stored: dict[str, Any]) -> SemanticOverrides:
    """The embedder's stored columns, by :func:`_validated`'s rule."""
    return _validated(SemanticOverrides, stored, "embedder")


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


@contextlib.asynccontextmanager
async def llm_settings_lifespan() -> AsyncIterator[None]:
    """ASGI lifespan hook: adopt the stored interpreter settings before serving.

    :func:`semantic_settings_lifespan`'s twin, and its own hook rather than two
    lines added to that one: the two settings fail independently — a database
    missing one table has no bearing on the other — and a single hook would
    make either failure swallow both adoptions.

    Only the web application registers it. The worker and the two commands read
    no questions, so there is nothing there for a stored interpreter to change.
    """
    await adopt_llm_settings()
    yield


def mail_config() -> MailConfig:
    return _registered(MailConfig)


def google_config() -> GmailConfig:
    return _registered(GmailConfig)


def imap_config() -> ImapConfig:
    return _registered(ImapConfig)


def m365_config() -> M365Config:
    return _registered(M365Config)


@lru_cache(maxsize=1)
def storage_reader() -> StorageReader:
    """What the archive occupies on disk, over the paths it actually uses.

    Built here because this is the only module that can name all three: the
    mailstore belongs to ``mailarc_core.archive``, the graph's data directory
    to ``mailarc_core.graph`` and the database file to appkit. The reader
    itself deliberately knows none of that — it measures whatever it is handed,
    in the order it is handed, and the labels are what a panel prints.

    A database configured in memory contributes no path at all rather than a
    zero: ``sqlite+aiosqlite:///:memory:`` is what the test profile uses, and a
    row reading "Database — 0 bytes" would describe a file that does not exist.
    """
    paths: dict[str, Path] = {
        "Mailstore": archive_config().store_dir,
        "Graph": graph_config().data_dir,
    }
    if (database := database_path(str(_registered(DatabaseConfig).url))) is not None:
        paths["Database"] = database
    return StorageReader(paths)


def publish_storage_reader() -> StorageReader:
    """Leave the reader where the dashboard's disk panel can find it.

    Same route and same reason as :func:`publish_graph_health`.
    """
    services = service_registry()
    reader = storage_reader()
    if services.has(StorageReader) and services.get(StorageReader) is reader:
        return reader
    services.register_as(StorageReader, reader)
    return reader


@lru_cache(maxsize=1)
def provider_registry() -> ProviderRegistry:
    """Every mailbox kind this installation can actually open.

    The registry is a singleton because it is a list of decisions, not state:
    building a second one would give two answers to "which providers exist".

    Registration order is the order the account form lists, so the first entry
    is what a new user is offered; new providers are appended and nothing above
    them moves.

    Every one is registered with ``using(config)`` rather than ``create``, and
    that is the rule this module exists for: ``create`` builds a fresh config
    off the environment, which would leave the one module allowed to build a
    component from configuration out of the loop — and for Microsoft 365 that
    config carries the installation's Entra client secret.

    **The consent argument is where the three real providers differ, and each
    answer is a fact about the provider rather than a convention.** Gmail has a
    browser round trip and nothing else, so it gets one. IMAP gets *none*: an
    app password is complete the moment it is typed, and this is the path that
    proves a provider with no consent runner works end to end — the account
    form writes ``mail_credentials.secret`` itself, as JSON over the
    descriptor's own fields. Microsoft 365 gets one runner for both of its
    modes, and the asymmetry lives inside it: a delegated sign-in opens a
    browser, an app-only grant was consented once by an administrator in the
    tenant and only has its tenant, mailbox and client secret checked. That
    branch belongs in the component, which owns the credential model that
    tells the two apart; registering no runner would tell the account page that
    Microsoft 365 has no second step, which is false for the mode most people
    use.
    """
    registry = ProviderRegistry()
    registry.register(FAKE_DESCRIPTOR, FakeMailSource.create)
    registry.register(
        GmailSource.DESCRIPTOR,
        GmailSource.using(google_config()),
        consent=gmail_consent,
    )
    registry.register(ImapSource.DESCRIPTOR, ImapSource.using(imap_config()))
    registry.register(
        M365Source.DESCRIPTOR,
        M365Source.using(m365_config()),
        consent=consent_runner(m365_config()),
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
    store under ``archive.store_dir`` — so what the search page lists is what
    the import wrote. Cached like the other handles: it holds no connection,
    but it is one decision and two objects would be two answers to "where is
    the archive".
    """
    return ArchiveReader(
        graph_session=partial(graph_session, graph_config()),
        blobs=BlobStore(archive_config()),
    )


def publish_archive_reader() -> ArchiveReader:
    """Leave the reader where the search page can find it.

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
def tag_store() -> TagStore:
    """The annotation layer, on the graph the archive reader reads.

    Deliberately the same graph and no second store: a tag is an annotation
    *on* ground truth, so a tag store pointed somewhere else would not read as a
    bug — it would read as an archive whose tags name messages it does not
    hold. Cached like the readers: one decision, and two objects would be two
    answers to "where are the tags".

    No blob half, unlike :func:`archive_reader`: everything the annotation layer
    writes is a node or an edge.
    """
    return TagStore(graph_session=partial(graph_session, graph_config()))


def publish_tag_store() -> TagStore:
    """Leave the tag store where the pages that tag can find it.

    Same route and same reason as :func:`publish_archive_reader`, down to the
    second call being a no-op: ``mailarc-ui`` may not import ``app``, and this
    is the one module allowed to build a component from configuration.
    """
    services = service_registry()
    store = tag_store()
    if services.has(TagStore) and services.get(TagStore) is store:
        return store
    services.register_as(TagStore, store)
    return store


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
def graph_reader() -> GraphReader:
    """The subgraph side of the same derived layer, on the same graph.

    A second reader rather than more methods on :func:`analytics_reader`,
    because the two answer different shapes: that one hands a page rows to put
    in a table, this one hands it nodes and edges to draw. The wiring is
    identical — one session factory, no store on disk — and deliberately the
    *same* graph, because the explorer draws a derived edge against the messages
    it was derived from.

    Cached for the reason every handle here is: one decision, and two objects
    would be two answers to "which graph is being explored".
    """
    return GraphReader(graph_session=partial(graph_session, graph_config()))


def publish_graph_reader() -> GraphReader:
    """Leave the reader where the graph explorer can find it.

    Same route and same reason as :func:`publish_analytics_reader`, down to the
    second call being a no-op: ``mailarc-ui`` may not import ``app``, and this
    is the one module allowed to build a component from configuration.
    """
    services = service_registry()
    reader = graph_reader()
    if services.has(GraphReader) and services.get(GraphReader) is reader:
        return reader
    services.register_as(GraphReader, reader)
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
    the same reason: a search result is a list of messages the reading pane is
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


def llm_config() -> LlmConfig:
    """The interpreter model this installation is configured with.

    Its own block rather than a field on
    :class:`~mailarc_analytics.semantic.config.SemanticConfig`, and the reason
    is what leaves the machine: embedding uploads message bodies once,
    interpreting uploads the sentence a person typed. An installation may
    reasonably want either without the other, which one shared ``provider``
    could not express.

    No stored-settings merge, unlike :func:`semantic_config`. There is no
    settings page for this yet, so the file and the environment are the whole
    of it — and :data:`~mailarc_analytics.nlq.errors.CONFIGURE_LLM` says so
    rather than naming a route that is not registered.
    """
    if _llm_override is not None:
        return _llm_override
    return _registered(LlmConfig)


@lru_cache(maxsize=1)
def chat_client() -> ChatPort | None:
    """This installation's interpreter model, or ``None`` because none is set.

    :func:`semantic_embedder`'s shape exactly, including the cache: the
    adapters hold an ``httpx`` connection pool, and a second one would pay a
    fresh handshake per question and never be closed.

    It inherits that function's one wart, which is worth naming rather than
    leaving to be discovered: **nothing closes this pool at shutdown.** The
    embedder's is closed only when a settings save replaces it, and this one
    has no settings page to be replaced from at all. Both are process-lifetime
    objects and the process exiting closes the sockets, so the cost is a
    warning on a hard stop rather than a leak that grows — mirroring it beats
    inventing a lifespan for one of the two clients.
    """
    return build_chat_client(llm_config())


@lru_cache(maxsize=1)
def query_interpreter() -> QueryInterpreter:
    """The natural-language reader, on this installation's model.

    **Built even when no model is configured**, for
    :func:`semantic_search`'s reason turned inside out. There it is because the
    working half must not be taken down with the unconfigured one; here it is
    because "no interpreter configured" is an *ordinary* state with a one-line
    remedy, and a page that could not look one up would show the half-wired
    -build sentence for it instead — a developer's error in front of a user,
    naming a fix that is not theirs to make.

    It reads no graph. Grounding is the caller's: the search page already holds
    the address, file and account options a question is matched against, and
    passing them in is what keeps this object constructible in a test with no
    application around it.
    """
    return QueryInterpreter(llm_config(), chat_client())


async def load_llm_config(
    session_factory: SessionFactory = get_asyncdb_session,
) -> LlmConfig:
    """Read the stored interpreter settings and adopt the merge as effective.

    :func:`load_semantic_config`'s twin, and the merge lives here for the same
    reason: §4.1 puts building a component from configuration in this module
    alone, and ``mailarc-analytics`` describes what an interpreter is without
    having to learn that a database holds one setting of it.

    Idempotent and cheap when nothing changed — a settings row that resolves to
    the configuration already in force returns without clearing a cache,
    without closing a client and without re-registering anything. With no row
    stored, :meth:`LlmOverrides.applied_to` hands back the configured object
    itself and this function is a no-op.
    """
    merged = (await _stored_llm_overrides(session_factory)).applied_to(
        _registered(LlmConfig)
    )
    if merged == llm_config():
        logger.debug("The stored interpreter settings change nothing")
        return merged
    await _adopt_llm_config(merged)
    # Both identifying values, never the key — nothing in this module logs one.
    logger.info(
        "Interpreter settings adopted: provider=%s model=%s",
        merged.provider,
        merged.model or "<the provider's default>",
    )
    return merged


async def _stored_llm_overrides(session_factory: SessionFactory) -> LlmOverrides:
    """The stored row as overrides, or nothing at all."""
    async with session_factory() as session:
        stored = await _LLM_SETTINGS.load(session)
    if stored is None:
        return LlmOverrides()
    return _validated_llm_overrides(
        {
            "provider": stored.provider,
            "model": stored.model,
            "base_url": stored.base_url,
            "api_key": stored.api_key,
        }
    )


def _validated_llm_overrides(stored: dict[str, Any]) -> LlmOverrides:
    """The interpreter's stored columns, by :func:`_validated`'s rule.

    The embedder's twin and now literally so. It used to be its own single
    retry that dropped the whole row on a second refusal and logged pydantic's
    message — with a stored ``api_key`` quoted inside it.
    """
    return _validated(LlmOverrides, stored, "interpreter")


async def _adopt_llm_config(merged: LlmConfig) -> None:
    """Make *merged* effective and close the client it replaces."""
    global _llm_override
    # Read before the swap, and only if one was ever built: asking for the
    # client here would otherwise construct one from the configuration being
    # replaced, just to close it again.
    stale = chat_client() if chat_client.cache_info().currsize else None
    _llm_override = merged
    chat_client.cache_clear()
    query_interpreter.cache_clear()
    if stale is not None:
        await stale.aclose()
    if service_registry().has(QueryInterpreter):
        publish_query_interpreter()


async def adopt_llm_settings() -> None:
    """Adopt the stored interpreter settings for this process, whatever happens.

    :func:`adopt_semantic_settings`' twin, with one fewer entry point to reach:
    only the web application reads a question, so the worker and the two
    commands have no reason to run this. It is still its own named step rather
    than two lines in the lifespan, because a second entry point that grows a
    reason to interpret should have one thing to call.

    A failure is logged and swallowed, and that is policy. The two real ones
    are a database whose migration has not been applied, so ``llm_settings``
    does not exist yet, and a row somebody edited by hand; neither is a reason
    to refuse to start. Both leave :func:`llm_config` answering with the file
    and the environment, which is a complete state (§7.4).
    """
    try:
        await load_llm_config()
    except Exception:
        logger.exception("Could not read the stored interpreter settings")


def publish_llm_control() -> LlmControl:
    """Leave the interpreter's two verbs where the settings page can find them.

    :func:`publish_semantic_control` minus its third verb. There is no
    ``reindex`` because nothing this model produces is stored — changing it
    costs the next question, so there is nothing to rebuild and nothing to warn
    about before a save.

    Registered once and never replaced: the entry is two references to
    module-level functions, so a second call has nothing new to say and
    overwriting it would only log noise.
    """
    services = service_registry()
    if services.has(LlmControl):
        return services.get(LlmControl)
    control = LlmControl(current=llm_config, reload=load_llm_config)
    services.register_as(LlmControl, control)
    return control


def publish_query_interpreter() -> QueryInterpreter:
    """Leave the interpreter where the search page can find it.

    Same route and same reason as :func:`publish_semantic_search`:
    ``mailarc-ui`` may not import ``app``, and building a client from
    configuration is this module's alone.
    """
    services = service_registry()
    interpreter = query_interpreter()
    if services.has(QueryInterpreter) and services.get(QueryInterpreter) is interpreter:
        return interpreter
    services.register_as(QueryInterpreter, interpreter)
    return interpreter


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


@lru_cache(maxsize=1)
def account_eraser() -> AccountEraser:
    """The clear-out side of an import, wired to this installation's stores.

    The same pair the worker writes with — the configured graph and the
    application's database — because clearing a mailbox has to reach exactly
    what importing it wrote, and a second answer to "which graph" would empty
    the wrong one.

    Cached like the other handles: it holds no connection, only the two
    factories that open one.
    """
    return AccountEraser(
        graph_session=partial(graph_session, graph_config()),
        database_session=get_asyncdb_session,
    )


def publish_account_eraser() -> AccountEraser:
    """Leave the eraser where the accounts page can find it.

    Same route and same reason as :func:`publish_archive_reader`, down to the
    second call being a no-op: ``mailarc-ui`` may not import ``app``, and this
    is the one module allowed to build a component from configuration.
    """
    services = service_registry()
    eraser = account_eraser()
    if services.has(AccountEraser) and services.get(AccountEraser) is eraser:
        return eraser
    services.register_as(AccountEraser, eraser)
    return eraser


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
