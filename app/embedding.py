"""Computing the vectors the import deliberately left empty — against this graph.

Here rather than in ``mailarc-analytics`` for the reason :mod:`app.derive` is
here: the semantic package knows how to embed an archive, it does not know
*which* archive, which embedder or which model. A component that read
configuration would have to know that configuration exists, and §4.1 leaves that
to the composition root. So this module is the half that says where — one
``GraphConfig``, one ``SemanticConfig``, one embedder, all three out of
:mod:`app.composition` — and :func:`mailarc_analytics.semantic.embed_pending` is
the half that says what.

Two callers, one function, exactly as with the rebuild. ``task graph:embed`` runs
``python -m app.embedding`` and waits; the ``embed`` job in :mod:`app.worker`
calls :func:`embed` and reports into its row. Both get the same run, because
there is only one, and a second way to start one would eventually be a second
way to configure it.

**The embedder-off case is not handled here.** ``embed_pending`` refuses a
``None`` embedder itself, with
:data:`~mailarc_analytics.semantic.errors.NO_EMBEDDER` — the sentence that names
the setting to change — and that sentence is what a user reads in the job row's
error column. Catching it here to log something friendlier would put the remedy
in a log file nobody has open and leave the row saying nothing.

Async all the way down, unlike the rebuild: the slow part of embedding is an
awaitable HTTP call and only the graph half blocks, which
``mailarc_analytics.semantic.indexing`` already puts in a thread of its own. So
there is no thread hop here and no bridge back to the loop.
"""

import asyncio
import logging

from app.composition import (
    adopt_semantic_settings,
    graph_config,
    semantic_config,
    semantic_embedder,
)
from app.configuration import configure
from mailarc_analytics.semantic import (
    CancelCheck,
    EmbedProgress,
    EmbedRun,
    embed_pending,
)
from mailarc_core.graph.client import session as graph_session

logger = logging.getLogger(__name__)


async def embed(
    on_progress: EmbedProgress | None = None,
    cancelled: CancelCheck | None = None,
) -> EmbedRun:
    """Embed every message that still needs it. Returns what the run did.

    The session factory is handed down rather than a session: the run opens one
    per graph call and closes it again, because a driver held across a hundred
    thousand HTTP round trips is a connection idle for most of the job's life —
    the same shape :func:`app.derive.rebuild` uses for the opposite reason
    (there the session is held for one blocking read, here it is not held at
    all).
    """
    run = await embed_pending(
        lambda: graph_session(graph_config()),
        semantic_embedder(),
        semantic_config(),
        on_progress=on_progress,
        cancelled=cancelled,
    )
    logger.info(
        "Embedding %s: %d of %d messages written, %d could not be embedded",
        "cancelled" if run.cancelled else "finished",
        run.done,
        run.total,
        run.failed,
    )
    return run


def main() -> int:
    """``python -m app.embedding``: embed once, and say so in the exit status.

    A failure is logged with its traceback and answered with a non-zero exit
    rather than a raised exception, because the caller is a shell task: what
    ``task graph:embed`` can act on is the status, and a stack trace on stderr
    is for the person reading afterwards. The embedder-off case is a failure
    here too — somebody who typed this command asked for vectors, and telling
    them nothing needed doing would be a lie.
    """
    logging.basicConfig(level=logging.INFO)
    # Importing `app` already configured the process; saying so here keeps this
    # command's dependency on configuration a statement rather than a side
    # effect. `configure` is cached, so this is free.
    configure()
    try:
        asyncio.run(_adopt_then_embed())
    except Exception:
        logger.exception("The archive could not be embedded")
        return 1
    return 0


async def _adopt_then_embed() -> EmbedRun:
    """The two steps, inside one loop, in the order that makes them mean anything.

    One :func:`asyncio.run` for both rather than two, because the second half
    of adoption is asynchronous — the stale embedder's ``httpx`` pool is closed
    with ``await`` — and a loop shut down between the two would take the pool
    the run is about to use down with it.

    Adopting *first* is the whole point: :func:`embed` reads
    :func:`~app.composition.semantic_embedder` and
    :func:`~app.composition.semantic_config` when it is called, so a command
    that read the row afterwards would have embedded with the configuration
    file's model and written that name onto every node.
    """
    await adopt_semantic_settings()
    return await embed()


if __name__ == "__main__":
    raise SystemExit(main())
