"""Rebuilding the vector index at the length this installation actually uses.

Here rather than in ``mailarc-analytics`` for the reason :mod:`app.embedding`
is here: the semantic package knows how to rebuild an index, it does not know
*which* graph or *which* length. That pair is configuration, and §4.1 leaves
configuration to the composition root.

**Why this exists at all, when there is a migration.** The vector index has one
fixed length, and since the embedder became a setting a human picks on
``/admin/embedder`` that length follows a model chosen at run time. A migration
is a versioned statement about the schema every installation shares — it cannot
express "whatever this installation chose", and re-running one revision with a
different constant is not a thing a migration chain can mean. So resizing is an
operation, not a revision, and this is where it is spelled.

What it costs is stated plainly rather than hidden: every stored vector is
forgotten, because a vector of the old length in an index of the new one is
accepted, never indexed, and indistinguishable from a good one. The embed job
afterwards recomputes them all. Nothing that came out of a message is touched —
``embedding`` and ``embedding_model`` are the semantic phase's own two
properties, left empty by the import.

Two callers, one function, as everywhere else in this application: ``task
graph:reindex-vectors`` runs ``python -m app.reindex`` and waits, and the
settings page calls :func:`reindex` and reports into the row a human is looking
at. A second way to start one would eventually be a second way to configure it.
"""

import asyncio
import logging
from functools import partial

from app.composition import (
    adopt_semantic_settings,
    graph_config,
    semantic_config,
)
from app.configuration import configure
from mailarc_analytics.semantic import rebuild_index
from mailarc_core.graph.client import session as graph_session

logger = logging.getLogger(__name__)


async def reindex(dimension: int | None = None) -> int:
    """Rebuild the index, and answer with how many vectors were forgotten.

    ``dimension`` defaults to the configured one, which is the case the
    settings page and the task both want: the length is already stored, and
    naming it twice is how the two drift apart. It is a parameter at all so a
    caller that has just been handed a number by a human — and has not yet
    saved it — can ask for that one instead.

    Off the event loop, because every runic call blocks. The rebuild is a
    handful of statements rather than a walk of the archive, but it is still
    the graph, and the rule does not have exceptions for short work.
    """
    wanted = semantic_config().dimension if dimension is None else dimension
    logger.info("Rebuilding the vector index at %d dimensions", wanted)
    cleared = await asyncio.to_thread(
        rebuild_index, partial(graph_session, graph_config()), wanted
    )
    logger.info(
        "Vector index rebuilt at %d dimensions; %d message(s) need embedding again",
        wanted,
        cleared,
    )
    return cleared


def main() -> int:
    """``python -m app.reindex``: rebuild once, and say so in the exit status.

    The same shape as :func:`app.embedding.main`, and for the same reason: the
    caller is a shell task, so what it can act on is the status. The settings
    are adopted first — the stored length is the one a human just chose, and
    rebuilding at the configuration file's length instead would produce an
    index that the very next embed job refuses.
    """
    logging.basicConfig(level=logging.INFO)
    # Importing `app` already configured the process; saying so keeps this
    # command's dependency on configuration a statement rather than a side
    # effect. `configure` is cached, so this is free.
    configure()
    try:
        asyncio.run(_adopt_then_reindex())
    except Exception:
        logger.exception("The vector index could not be rebuilt")
        return 1
    return 0


async def _adopt_then_reindex() -> int:
    """Adopt the stored settings, then rebuild — in one loop, in that order.

    One :func:`asyncio.run` for both because adoption closes a stale embedder's
    ``httpx`` pool with ``await``, and a loop shut down between the two would
    take that pool down mid-close.
    """
    await adopt_semantic_settings()
    return await reindex()


if __name__ == "__main__":
    raise SystemExit(main())
