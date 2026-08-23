"""Recomputing the derived layer: once, now, against the configured graph.

Here rather than in ``mailarc-analytics`` for the reason ``app/worker.py`` is
here: the analysis knows how to rebuild, it does not know *which* graph. A
component that opened its own session would have to read configuration, and
§4.1 leaves that to the composition root. So this module is the half that says
where — one ``GraphConfig``, one ``AnalyticsConfig``, both out of
``app.composition`` — and :func:`mailarc_analytics.rebuild_derived` is the half
that says what.

It is also where A2's **sixth signal** is joined on, and that is the same rule
seen from the other side. ``derived/`` may not import ``semantic/`` — a test in
``mailarc-analytics`` proves it, because the day it could, ``ABOUT.method``
would stop reliably meaning "a fact taken from a header" — so the neighbours
are read here and handed in as :class:`~mailarc_analytics.derived.model.
SimilarityEdge` values. This module is the only layer allowed to name both.
With no embedder configured it hands in nothing and the rebuild is exactly the
phase-5 one; see :func:`_semantic_edges`.

Two callers, one function. ``task graph:rebuild-derived`` runs
``python -m app.derive`` and waits; the ``derive`` job in ``app/worker.py``
calls :func:`rebuild` inside ``asyncio.to_thread`` and reports into its row.
Both get the same rebuild, because there is only one, and a second way to
start one would eventually be a second way to configure it.

The command prints nothing. What a rebuild found belongs in the log next to
what the worker logs about the same work, not on a stdout only one of the two
callers has.

Two lines and every one of :class:`~mailarc_analytics.DerivedCounts`'s fourteen
counts in them, each behind a phrase that names it. ``/admin/insights`` enqueues
a ``derive`` job and follows the row, but a row reports stages and not buckets —
so these two lines stay the only place the fourteen counts are written down, and
a run that read a tenth of the archive, or dropped a bucket, has to be
distinguishable there from one that found nothing to say.
"""

import asyncio
import logging
from collections.abc import Sequence

from runic.ogm import Session

from app.composition import (
    adopt_semantic_settings,
    analytics_config,
    graph_config,
    semantic_config,
    semantic_embedder,
)
from app.configuration import configure
from mailarc_analytics import DerivedCounts, ProgressHook, rebuild_derived
from mailarc_analytics.derived.model import EMBEDDING_METHOD, SimilarityEdge
from mailarc_analytics.semantic import similar_pairs
from mailarc_core.graph.client import session as graph_session

logger = logging.getLogger(__name__)


def rebuild(on_progress: ProgressHook | None = None) -> DerivedCounts:
    """Delete the derived layer and compute it again. Returns what it did.

    Blocking, because every runic driver is: an async caller wraps this in
    ``asyncio.to_thread``, and ``on_progress`` is then called from that thread
    once per stage.

    The session is opened and closed around the one rebuild rather than kept:
    a rebuild is a command, not a service, and a driver held open between two
    of them would be a connection nobody is using while the archive is idle.
    """
    with graph_session(graph_config()) as session:
        counts = rebuild_derived(
            session,
            analytics_config(),
            on_progress=on_progress,
            extra_edges=_semantic_edges(session),
        )
    logger.info(
        "Derived layer rebuilt from %d messages: %d groups, %d co-addressed pairs, "
        "%d topics, %d templates (%d nodes and %d edges removed first)",
        counts.messages,
        counts.groups,
        counts.co_addressed,
        counts.topics,
        counts.templates,
        counts.deleted_nodes,
        counts.deleted_edges,
    )
    logger.info(
        "Left out: %d messages beyond the ceiling, %d with no canonical id, "
        "%d addressed too widely for a pair, %d with no fingerprint; "
        "%d signal buckets, %d band buckets and %d weak pairs dropped",
        counts.beyond_ceiling,
        counts.unidentified,
        counts.wide_messages,
        counts.unhashable_messages,
        counts.dropped_buckets,
        counts.dropped_template_buckets,
        counts.dropped_weak_pairs,
    )
    return counts


def _semantic_edges(session: Session) -> Sequence[SimilarityEdge]:
    """A2's sixth signal, or nothing at all because no embedder is configured.

    This is the joint §10 phase 6 item 3 asks for, and it is here because this
    is the only module allowed to name both sides of it: ``derived`` may not
    import ``semantic`` — a test in ``mailarc-analytics`` proves it, because the
    moment it could, ``ABOUT.method`` would stop reliably meaning "a fact from a
    header" — and ``semantic`` has no business knowing what a topic is.

    ``provider=none`` is the default and returns ``()``, which makes the rebuild
    byte-for-byte the phase-5 one. That is not a convenience; it is the DoD's
    "all phase-5 analyses run unchanged", and the pin for it is in
    ``tests/test_derive.py``.

    A failure here is logged and swallowed, which nothing else in this module
    does. Signal 6 is the one signal that is a *suggestion* — the other five are
    facts out of the headers — so losing it costs a few topic memberships,
    while letting it propagate would cost the whole derived layer over an
    un-upgraded graph or a model server that was not running. The rebuild that
    follows is still complete, correct and idempotent; it just has five signals
    instead of six, which is exactly what every installation without an embedder
    has.

    The session is the rebuild's own, so the neighbours and the facts are read
    from one instant of an archive that may be being imported into at the same
    time.
    """
    embedder = semantic_embedder()
    if embedder is None:
        return ()
    settings = semantic_config()
    try:
        pairs = similar_pairs(
            session,
            model=embedder.model,
            neighbours=settings.topic_neighbours,
            minimum=settings.topic_similarity_min,
            limit=analytics_config().topic_max_weak_pairs,
        )
    except Exception as error:
        logger.warning(
            "A2 runs on its five exact signals: the neighbour read failed (%s)", error
        )
        return ()
    logger.info(
        "A2 signal 6 offers %d semantic pairs at or above %.2f from %s",
        len(pairs),
        settings.topic_similarity_min,
        embedder.model,
    )
    return tuple(
        SimilarityEdge(
            left=one.left, right=one.right, method=EMBEDDING_METHOD, weight=one.score
        )
        for one in pairs
    )


def main() -> int:
    """``python -m app.derive``: rebuild once, and say so in the exit status.

    A failure is logged with its traceback and answered with a non-zero exit
    rather than a raised exception, because the caller is a shell task: what
    ``task graph:rebuild-derived`` can act on is the status, and a stack trace
    on stderr is for the person reading afterwards.
    """
    logging.basicConfig(level=logging.INFO)
    # Importing `app` already configured the process; saying so here keeps this
    # command's dependency on configuration a statement rather than a side
    # effect. `configure` is cached, so this is free.
    configure()
    try:
        # Before the rebuild, not inside it: `_semantic_edges` reads the
        # embedder and the settings when A2 asks for signal 6, and in a fresh
        # command process neither has seen the stored row. A rebuild that
        # skipped this quietly runs on five signals — or asks the graph for
        # neighbours in a space the archive was never embedded in — and writes
        # a derived layer that differs from the worker's for the same archive.
        # Its own `asyncio.run`, because `rebuild` is blocking and belongs
        # outside a loop.
        asyncio.run(adopt_semantic_settings())
        rebuild()
    except Exception:
        logger.exception("The derived layer could not be rebuilt")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
