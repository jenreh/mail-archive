"""Finding a message, checking what the analyses found, and believing A1.

``model``
    The views a catalogue row or a search hit is projected onto — no graph
    node and no ORM entity ever reaches the browser — and the formatting that
    decides what a cell says. Knows no I/O, so it is checkable without a
    graph.
``state``
    ``AnalyticsInsightsState`` over ``mailarc_analytics``'s ``AnalyticsReader``
    and ``mailarc_sync``'s job queue: everything that reads, polls or holds the
    state lock.
``search``
    ``ArchiveSearchState`` over ``mailarc_analytics``'s ``SemanticSearch``:
    the box, its two paths, and what the panel says when the semantic one is
    off — which, with ``provider`` defaulting to ``none``, is what a fresh
    installation sees. A state of its own because it reads through a
    different service and must keep working when the derived layer does not.
``components``
    The panels a page drops in: the rebuild control, the search box, the
    counts, the co-addressed cross-check, and one listing per analysis.

The cross-check is why this exists. A page that only listed findings would say
what the derived layer holds; running A1's definition against A1's materialised
edge says whether any of it is true.
"""

from mailarc_ui.insights.components import (
    agreement_card,
    analyses,
    disputes_table,
    groups_card,
    guidance_panel,
    hits_table,
    insights_panel,
    pairs_table,
    rebuild_card,
    rebuild_controls,
    search_card,
    templates_card,
    topics_card,
    totals_card,
)
from mailarc_ui.insights.model import (
    AgreementView,
    DisputeView,
    Found,
    GroupView,
    HitView,
    PairView,
    RebuildJobView,
    TemplateView,
    TopicView,
    TotalsView,
    found_summary,
    method_color,
    sample_label,
    short_key,
    span_label,
)
from mailarc_ui.insights.search import ArchiveSearchState, archive_search
from mailarc_ui.insights.state import AnalyticsInsightsState, analytics_reader

__all__ = [
    "AgreementView",
    "AnalyticsInsightsState",
    "ArchiveSearchState",
    "DisputeView",
    "Found",
    "GroupView",
    "HitView",
    "PairView",
    "RebuildJobView",
    "TemplateView",
    "TopicView",
    "TotalsView",
    "agreement_card",
    "analyses",
    "analytics_reader",
    "archive_search",
    "disputes_table",
    "found_summary",
    "groups_card",
    "guidance_panel",
    "hits_table",
    "insights_panel",
    "method_color",
    "pairs_table",
    "rebuild_card",
    "rebuild_controls",
    "sample_label",
    "search_card",
    "short_key",
    "span_label",
    "templates_card",
    "topics_card",
    "totals_card",
]
