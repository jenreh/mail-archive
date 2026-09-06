"""What the header asks the archive about the open message, and nothing else.

The seam :mod:`mailarc_ui.search.reads` established: what is here touches the
service registry, takes no state lock, writes no var and is callable from a
test with no Reflex state at all. What stays in
:mod:`mailarc_ui.message_detail.state` is the Reflex class.

Three archives answer, and the split is the architecture's — the same one
:mod:`mailarc_ui.search.memberships` documents. A tag is annotation on ground
truth and comes from :class:`~mailarc_core.archive.tags.TagStore`; a topic and
a recurring group are derived, deleted and recomputed by every rebuild, and
come from :class:`~mailarc_analytics.AnalyticsReader`. The two lookups are
imported from :mod:`mailarc_ui.tags.reads` rather than written again, which is
also what keeps the dependency between the two packages one-way.

**Each read fails on its own.** A header is not a page: an archive nobody has
rebuilt has no topics and no groups, and a graph that is down has none of the
three. Either way the mail still has to be readable, so a read that raises
costs its own chips and nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from mailarc_analytics import GroupMembershipRow, TopicMembershipRow
from mailarc_core.archive.model import TagSummary
from mailarc_ui.message_detail.model import MessageChip, chips_for
from mailarc_ui.tags.reads import analytics_reader, tag_store

logger = logging.getLogger(__name__)

__all__ = ["chips_of"]


def chips_of(message_id: str) -> list[MessageChip]:
    """Every topic, tag and group the message sits in, as the header shows them.

    Blocking — all three drivers behind it are — so the caller runs it in a
    thread. An empty id asks nothing.
    """
    if not message_id:
        return []
    return chips_for(_topic(message_id), _tags(message_id), _group(message_id))


def _topic(message_id: str) -> TopicMembershipRow | None:
    try:
        return analytics_reader().topics_of([message_id]).get(message_id)
    except Exception:
        logger.exception("Could not read the topic of %s", message_id)
        return None


def _tags(message_id: str) -> Sequence[TagSummary]:
    try:
        return tag_store().tags_of([message_id]).get(message_id, ())
    except Exception:
        logger.exception("Could not read the tags of %s", message_id)
        return ()


def _group(message_id: str) -> GroupMembershipRow | None:
    try:
        return analytics_reader().groups_of([message_id]).get(message_id)
    except Exception:
        logger.exception("Could not read the group of %s", message_id)
        return None
