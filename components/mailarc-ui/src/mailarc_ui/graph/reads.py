"""Everything the explorer reaches outside itself for, and nothing else.

The seam :mod:`mailarc_ui.dashboard.reads` established: what is here touches the
service registry and the graph, takes no state lock, touches no var, and is
callable from a test with no Reflex state at all. What is left in
:mod:`mailarc_ui.graph.state` is the Reflex class.

Four services for one page, which is more than any other page here reaches for
and is the honest count: the picture comes from
:class:`~mailarc_analytics.queries.graphs.GraphReader`, the *dropdown* over it
from :class:`~mailarc_analytics.AnalyticsReader` and
:class:`~mailarc_core.archive.tags.TagStore`, and opening a message in the pane
beside the canvas from :class:`~mailarc_core.archive.reader.ArchiveReader`. Each
is looked up inside the function that needs it — ``mailarc-ui`` may not import
``app`` (§6), and a lookup at module level would run while ``app/app.py`` is
still being imported, before anything had been published.

The two lookups this page shares with the tag actions are imported from
:mod:`mailarc_ui.tags.reads` rather than written again. That is also the
direction of the dependency between the two packages — the explorer's state
*is* a tag actions host — so it can never become a cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from mailarc_analytics import NodeKind, Subgraph
from mailarc_analytics.queries.graphs import GraphReader
from mailarc_ui.graph.model import GraphView
from mailarc_ui.message_detail import archive_reader
from mailarc_ui.tags.reads import (
    analytics_reader,
    answered,
    published,
    tag_store,
)

logger = logging.getLogger(__name__)

PICKER_LIMIT = 40
"""How many things one dropdown offers to root a view at.

A picker is somewhere to start rather than a listing: the archive's forty
biggest topics are a place to look, and its four thousand are a scrollbar.
"""

MEMBER_LIMIT = 500
"""How many messages :func:`cluster_members` will promote in one gesture.

Higher than a picture is ever drawn at, because this is not a picture — it is
the membership of a tag, and a tag that quietly held the first two hundred of a
topic would be a project label nobody could trust.
"""

__all__ = [
    "MEMBER_LIMIT",
    "PICKER_LIMIT",
    "analytics_reader",
    "answered",
    "archive_reader",
    "cluster_members",
    "graph_reader",
    "picker_options",
    "tag_store",
    "view_of",
]


def graph_reader() -> GraphReader:
    """The subgraph reader the composition root published."""
    return published(GraphReader, "graph reader")


def view_of(value: GraphView | str) -> GraphView:
    """*value* as a view, or the overview.

    A view name arrives from a query parameter, which means it arrives from a
    bookmark somebody saved before a view was renamed. An unknown one is the
    map rather than a page that fails to load.
    """
    try:
        return GraphView(value)
    except ValueError:
        logger.debug("Unknown graph view %r — showing the overview", value)
        return GraphView.OVERVIEW


def picker_options(view: GraphView | str) -> list[dict[str, str]]:
    """What a view of this kind can be rooted at, as a dropdown takes it.

    Empty for the overview, which is rooted at nothing, and empty for a listing
    that could not be read: a picker with nothing in it is a page that still
    draws, and the panel's own error line is where an outage belongs. Every one
    of these is a listing the archive already answers for another page.
    """
    kind = view_of(view)
    listing = _PICKERS.get(kind)
    if listing is None:
        return []
    try:
        return listing()
    except Exception:
        logger.exception("Could not list what a %s view roots at", kind.value)
        return []


def cluster_members(kind: str, cluster_id: str) -> tuple[str, ...]:
    """Every message in one topic or one circle, for a tag to be made of.

    Read afresh rather than taken off the picture on screen: what is drawn is
    capped at a canvas' worth of nodes and may have been reached by expanding
    something else entirely, and a tag is the durable thing here (R7). Half a
    project is worse than none.
    """
    reader = graph_reader()
    if kind == GraphView.TOPIC.value:
        found: Subgraph = reader.topic(cluster_id, limit=MEMBER_LIMIT)
    elif kind == GraphView.COMMUNITY.value:
        found = reader.community(cluster_id, limit=MEMBER_LIMIT)
    else:
        logger.debug("Nothing to promote from a %s", kind)
        return ()
    return tuple(one.id for one in found.nodes if one.kind is NodeKind.MESSAGE)


def _topic_options() -> list[dict[str, str]]:
    """One entry per topic, however many signals drew it.

    ``AnalyticsReader.topics`` is one row per topic *per signal* — a cluster
    joined both by a ticket token and by its participants comes back twice —
    and a dropdown holding the same topic three times is one nobody can pick
    from. The first row wins, which is the biggest, because the listing is
    ordered by messages.
    """
    found: dict[str, dict[str, str]] = {}
    for row in analytics_reader().topics(limit=PICKER_LIMIT):
        found.setdefault(
            row.id,
            {
                "value": row.id,
                "label": f"{row.label or '(no subject in common)'} ({row.messages})",
            },
        )
    return list(found.values())


def _community_options() -> list[dict[str, str]]:
    return [
        {
            "value": row.id,
            "label": f"{row.label or '(no common domain)'} ({row.size})",
        }
        for row in analytics_reader().communities(limit=PICKER_LIMIT)
    ]


def _tag_options() -> list[dict[str, str]]:
    return [
        {"value": one.id, "label": f"{one.name or one.id} ({one.message_count})"}
        for one in tag_store().list_tags()
    ]


def _message_options() -> list[dict[str, str]]:
    """The newest mail, which is where somebody exploring one starts."""
    return [
        {"value": one.id, "label": one.subject or "(no subject)"}
        for one in archive_reader().list_messages(limit=PICKER_LIMIT)
    ]


def _address_options() -> list[dict[str, str]]:
    """The correspondents the archive writes to together the most.

    Off the materialised ``CO_ADDRESSED`` edge rather than off the self-join
    behind it: this is a dropdown, and the self-join is the one read on the
    insights page whose cost grows with the mailbox. An address book has no
    "biggest" of its own, so the pairs are what names the people worth starting
    at.
    """
    found: dict[str, dict[str, str]] = {}
    for row in analytics_reader().top_co_addressed(limit=PICKER_LIMIT):
        for one in (row.left_id, row.right_id):
            found.setdefault(one, {"value": one, "label": one})
    return list(found.values())


_PICKERS: dict[GraphView, Callable[[], list[dict[str, str]]]] = {
    GraphView.TOPIC: _topic_options,
    GraphView.COMMUNITY: _community_options,
    GraphView.TAG: _tag_options,
    GraphView.MESSAGE: _message_options,
    GraphView.ADDRESS: _address_options,
}
"""What fills the picker for each view that has a root. The overview has none,
which is why it is absent rather than mapped to something that answers ``[]``:
a view with nothing to pick and a view whose listing is empty are different
states, and only the first has no dropdown at all."""
