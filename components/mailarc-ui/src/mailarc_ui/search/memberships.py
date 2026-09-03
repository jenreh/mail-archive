"""Which group each message of a page sits in — the read beside the page.

Five groupings need the archive to say where a row belongs, and this module is
where the five reads live: one function, one blocking call per page, and a
:class:`~mailarc_ui.search.model.Membership` per message on the way out. It
holds no state lock and touches no var — a background handler calls it from a
thread, the way :mod:`mailarc_ui.search.reads` is called — so it is checkable
against the fakes the state tests already publish.

Three archives answer, and the split is the architecture's. A conversation and
a recipient are ground truth and come from
:class:`~mailarc_core.ArchiveReader`; a tag is annotation on ground truth and
comes from :class:`~mailarc_core.TagStore`; a topic and a recurring group are
derived, deleted and recomputed by every rebuild, and come from
:class:`~mailarc_analytics.AnalyticsReader`. A page grouped by topic on an
archive nobody has rebuilt is every row under "No topic", which is the truth.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from mailarc_ui.search import reads
from mailarc_ui.search.model import READ_GROUPINGS, Grouping, Membership


def read_memberships(ids: Sequence[str], grouping: Grouping) -> dict[str, Membership]:
    """Where each of these messages sits under *grouping*, keyed by message id.

    Blocking — every driver behind it is — so the caller runs it in a thread.
    Nothing is read for a grouping that needs no read, and an empty ask never
    opens a session. A message the read did not file is absent, and it is
    :func:`~mailarc_ui.search.model.filed_by` that puts it in the bucket.
    """
    asked = list(dict.fromkeys(ids))
    if not asked or grouping not in READ_GROUPINGS:
        return {}
    return _READERS[grouping](asked)


def _conversations(ids: list[str]) -> dict[str, Membership]:
    found = reads.archive_reader().conversations_of(ids)
    return {one: Membership.of_conversation(found[one]) for one in found}


def _recipients(ids: list[str]) -> dict[str, Membership]:
    found = reads.archive_reader().recipients_of(ids)
    return {one: Membership.of_recipient(found[one]) for one in found}


def _tags(ids: list[str]) -> dict[str, Membership]:
    found = reads.tag_store().tags_of(ids)
    filed = {one: Membership.of_tags(tags) for one, tags in found.items()}
    return {one: membership for one, membership in filed.items() if membership}


def _topics(ids: list[str]) -> dict[str, Membership]:
    found = reads.analytics_reader().topics_of(ids)
    return {one: Membership.of_topic(found[one]) for one in found}


def _groups(ids: list[str]) -> dict[str, Membership]:
    found = reads.analytics_reader().groups_of(ids)
    return {one: Membership.of_group(found[one]) for one in found}


_READERS: dict[Grouping, Callable[[list[str]], dict[str, Membership]]] = {
    Grouping.CONVERSATION: _conversations,
    Grouping.RECEIVER: _recipients,
    Grouping.TAG: _tags,
    Grouping.TOPIC: _topics,
    Grouping.RECURRING: _groups,
}
"""One read per grouping that needs one — the same set :data:`READ_GROUPINGS`
names, which a test checks."""
