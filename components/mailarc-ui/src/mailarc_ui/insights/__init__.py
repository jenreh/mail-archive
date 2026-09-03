"""Checking what the analyses found, and believing A1.

``model``
    The views a catalogue row is projected onto — no graph node and no ORM
    entity ever reaches the browser — and the formatting that decides what a
    cell says. Knows no I/O, so it is checkable without a graph.
``state``
    ``AnalyticsInsightsState`` over ``mailarc_analytics``'s ``AnalyticsReader``
    and ``mailarc_sync``'s job queue: everything that reads, polls or holds the
    state lock.
``components``
    The panels a page drops in: the rebuild control, the counts, the
    co-addressed cross-check, and one listing per analysis — the three the
    first phase wrote, plus the circles, what probably matters, and the tags.

The tags card is the odd one and worth naming. Everything else here reports
the derived layer, which a rebuild deletes and computes again; a ``Tag`` is
annotation on ground truth and survives. The handlers behind it are
:class:`~mailarc_ui.tags.state.TagActionsState`, mixed into
``AnalyticsInsightsState`` rather than written twice — the graph explorer hosts
the same mixin — so a tag deleted on either page is deleted on both.

There was a third module here, ``search`` — a box over ``SemanticSearch`` from
when this page was the only place to find a message. ``/`` is that place now,
with filters and a reading pane, and a Reflex state is not free to keep: every
one of them is built into the state tree of every session, whether or not a
page renders it.

The cross-check is why this exists. A page that only listed findings would say
what the derived layer holds; running A1's definition against A1's materialised
edge says whether any of it is true.
"""

from mailarc_ui.insights.components import (
    agreement_card,
    analyses,
    communities_card,
    disputes_table,
    groups_card,
    guidance_panel,
    important_card,
    insights_panel,
    pairs_table,
    rebuild_card,
    rebuild_controls,
    rebuild_hint,
    tags_card,
    templates_card,
    topics_card,
    totals_card,
)
from mailarc_ui.insights.model import (
    AgreementView,
    CommunityView,
    DisputeView,
    GroupView,
    ImportantMessageView,
    PairView,
    RebuildJobView,
    TemplateView,
    TopicView,
    TotalsView,
    method_color,
    sample_label,
    short_key,
    span_label,
)
from mailarc_ui.insights.state import AnalyticsInsightsState, analytics_reader

__all__ = [
    "AgreementView",
    "AnalyticsInsightsState",
    "CommunityView",
    "DisputeView",
    "GroupView",
    "ImportantMessageView",
    "PairView",
    "RebuildJobView",
    "TemplateView",
    "TopicView",
    "TotalsView",
    "agreement_card",
    "analyses",
    "analytics_reader",
    "communities_card",
    "disputes_table",
    "groups_card",
    "guidance_panel",
    "important_card",
    "insights_panel",
    "method_color",
    "pairs_table",
    "rebuild_card",
    "rebuild_controls",
    "rebuild_hint",
    "sample_label",
    "short_key",
    "span_label",
    "tags_card",
    "templates_card",
    "topics_card",
    "totals_card",
]
