"""What this page reads and writes outside its own state, and nothing else.

Split out of :mod:`mailarc_ui.embedder.state` when that module passed §5's
thousand-line cap, along the seam the module docstring there already names.
What is left in ``state`` is the Reflex class: vars, handlers, the state lock
and what the form shows. What is here is the four things that reach *out* of
it — the service registry, the archive's database, the graph, and a string a
browser sent — none of which take a lock, none of which touch a var, and every
one of which is callable from a test without a Reflex state at all.

The seam matters beyond the line count. A handler is wrong when it holds a lock
across an await or blanks a control on a dropped read; a read is wrong when it
opens a graph the composition root did not choose, or lets a failure that a
settings page must survive escape. Those are different mistakes and they are
now in different files.
"""

import asyncio
import logging
from datetime import datetime
from urllib.parse import urlsplit

from appkit_commons.database.session import get_asyncdb_session
from appkit_commons.registry import service_registry

from mailarc_analytics.semantic import SemanticControl, SemanticSearch
from mailarc_core.database.entities import SEMANTIC_SETTINGS_ID, SemanticSettingsEntity
from mailarc_core.database.repositories import SemanticSettingsRepository
from mailarc_ui.embedder.model import NO_CONTROL

logger = logging.getLogger(__name__)

SETTINGS = SemanticSettingsRepository()
"""The one repository this page writes through. Stateless, so one is enough."""


def semantic_control() -> SemanticControl:
    """The composition root's two verbs. Call inside a method only.

    Read out of the registry the way
    :func:`~mailarc_ui.insights.state.analytics_reader` reads its reader:
    ``mailarc-ui`` may not import ``app`` (§4.1), and merging a stored row over
    a configuration file is the composition root's work. Never at module level
    — that would run before the application had filled the registry.
    """
    try:
        return service_registry().get(SemanticControl)
    except KeyError as error:
        raise RuntimeError(NO_CONTROL) from error


def is_absolute_http(url: str) -> bool:
    """Whether *url* is an absolute ``http``/``https`` URL with a host in it.

    Both halves checked: a bare ``gpu.internal:11434`` parses with
    ``scheme='gpu.internal'`` and no netloc, which is what this refuses. The
    value decides which host receives this archive's stored bearer token on
    every embedding call, and it arrives over a socket.
    """
    parsed = urlsplit(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


async def write_settings(
    settings: tuple[str, str, int, str],
    api_key: str,
    baseline: datetime | None,
) -> None:
    """The four settings and, only if one was typed, the key — in one transaction.

    One session for both, so a key that cannot be encrypted takes the settings
    down with it. The alternative is a stored row describing an OpenAI embedder
    whose credential was never written, which fails later, elsewhere, as a 401.

    ``baseline`` is the row's ``updated`` as the form last read it, and turns
    the write into "store this if the row is still the one I read" — see
    :class:`~mailarc_core.database.repositories.SettingsChangedElsewhere` for
    the two-administrator case that costs. ``None`` means the form read no row,
    which is what a fresh installation honestly is.

    ``api_key`` empty is not passed on at all, and that is the write-only
    rule's other half:
    :meth:`~mailarc_core.database.repositories.SemanticSettingsRepository.store`
    has no parameter for it, so "leave the stored key alone" is the only thing
    an empty box can mean.
    """
    provider, model, dimension, base_url = settings
    async with get_asyncdb_session() as session:
        await SETTINGS.store(
            session,
            provider=provider,
            model=model,
            dimension=dimension,
            base_url=base_url,
            expected_updated=baseline,
        )
        if api_key:
            await SETTINGS.set_api_key(session, api_key)


async def stored_baseline() -> tuple[bool, datetime | None]:
    """Whether a key is stored, and the row's timestamp — in one read.

    Two questions of the same row, so one session and one ``SELECT`` rather
    than two. Neither answer is the key: ``api_key_is_set`` is an ``IS NOT
    NULL`` the database evaluates, and ``updated`` is a timestamp. The
    ciphertext is not fetched, not decrypted and not in this process, which is
    the rule the whole page is built on — ``load`` is the composition root's
    read and is deliberately not called from here.
    """
    async with get_asyncdb_session() as session:
        keyed = await SETTINGS.api_key_is_set(session)
        row = await session.get(SemanticSettingsEntity, SEMANTIC_SETTINGS_ID)
        return keyed, row.updated if row is not None else None


async def from_the_graph() -> tuple[int, int, bool]:
    """What is embedded and what the live index will index, if either is readable.

    Through the published :class:`~mailarc_analytics.semantic.search.SemanticSearch`
    rather than a graph session of its own: opening a graph is building a
    component from configuration, and the search is already in the registry
    holding exactly the session factory the composition root chose.

    Both answers under one flag, because they come off the same graph and fail
    together. The index length is read rather than taken from the configuration
    for the reason ``indexing.verify`` and ``SemanticSearch._knn`` read it: the
    configuration is exactly what can be wrong, and a vector whose length the
    index does not carry is stored, never indexed, and reported nowhere.

    Failing quietly is the policy for both — a settings page has to work on an
    installation whose graph is not running, because configuring the embedder
    is what you do before it works — and an unread value produces a stronger
    warning rather than none.

    Blocking work, so it goes to a thread: every runic driver blocks, and this
    page must not stall the event loop counting somebody's archive.
    """
    try:
        search = service_registry().get(SemanticSearch)
    except KeyError:
        logger.warning("No search is registered; not reading the vector state")
        return 0, 0, False
    try:
        counted, index = await asyncio.to_thread(_both, search)
    except Exception:
        logger.exception("Could not read the vector state")
        return 0, 0, False
    return counted, index, True


def _both(search: SemanticSearch) -> tuple[int, int]:
    """The two blocking reads, in the one thread hop they share."""
    return search.coverage().embedded, search.index_dimension()
