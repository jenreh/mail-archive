"""Everything the tag actions reach outside themselves for, and nothing else.

The seam :mod:`mailarc_ui.dashboard.reads` established, for a mixin rather than
a page: what is here touches the service registry and the graph, takes no state
lock, touches no var, and is callable from a test with no Reflex state at all.

Two services and no more. The annotation layer itself is
:class:`~mailarc_core.archive.tags.TagStore` — ``mailarc-core``, because a tag
is an annotation on ground truth — and what is *offered* comes from
:class:`~mailarc_analytics.AnalyticsReader`, because a suggestion is derived and
the next rebuild deletes and recomputes it. That split is the whole reason
tagging needs two objects rather than one.

Both are looked up inside the function that needs one. ``mailarc-ui`` may not
import ``app`` (§6), so every object the composition root built arrives through
the registry, and a lookup at module level would run while ``app/app.py`` is
still being imported — before anything had been published.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from appkit_commons.registry import service_registry

from mailarc_analytics import AnalyticsReader
from mailarc_core.archive import TagStore

logger = logging.getLogger(__name__)


def tag_store() -> TagStore:
    """The annotation layer the composition root published."""
    return published(TagStore, "tag store")


def analytics_reader() -> AnalyticsReader:
    """The derived layer's read side — where a suggestion comes from."""
    return published(AnalyticsReader, "analytics reader")


async def answered[T](work: Callable[[], T], what: str, empty: T) -> tuple[T, str]:
    """One blocking call, off the event loop, with an outage as its answer.

    Every runic driver blocks, so the work goes to a thread. What comes back
    from a failure is a sentence for the panel that asked rather than an
    exception: a graph that went away is a state the page has to render, and
    one dead panel must not take the rest of the page with it.

    The same shape :mod:`mailarc_ui.insights.state` uses, written once here
    because the graph explorer needs it too and neither of those two modules
    can import the other.
    """
    try:
        return await asyncio.to_thread(work), ""
    except Exception as error:
        logger.exception("Could not %s", what)
        return empty, str(error) or type(error).__name__


def published[T](kind: type[T], what: str) -> T:
    """One service out of the registry, or a sentence naming what is missing."""
    try:
        return service_registry().get(kind)
    except KeyError as error:
        raise RuntimeError(
            f"No {what} is registered — did app.composition run?"
        ) from error
