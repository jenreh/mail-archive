"""The welcome dashboard: what the archive looks like at a glance.

Four files with the roles §6 fixes. ``model`` is the value objects and every
string the page prints, and knows no I/O. ``reads`` is everything that reaches
outside the process — the registry, the archive's database, the graph, the
disk. ``state`` is the Reflex class, the six panels' error isolation, and the
gate that keeps two of those panels off a public page. ``components`` draws it.

The one thing to read before changing anything here is the module docstring of
:mod:`mailarc_ui.dashboard.state`: ``/`` is public by design, and which half of
this page is public is a decision recorded there rather than a property of the
decorator on the page.
"""

from mailarc_ui.dashboard.components import dashboard_panel, kpi_band
from mailarc_ui.dashboard.model import (
    DEFAULT_RANGE,
    NOTIFICATION_LIMIT,
    NOTIFICATION_SHOWN,
    RANGE_DAYS,
    UNKNOWN,
    DashboardCounts,
    MeterView,
    NotificationView,
    Readout,
    ServiceView,
    VectorState,
    chosen_range,
    day_label,
    days_in,
    dismissed_keys,
    gigabytes,
    health_meters,
    human_bytes,
    last_archived_label,
    messages_points,
    moment_label,
    notice_key,
    notifications_of,
    percent_label,
    ratio_percent,
    remembering,
    services_of,
    storage_meters,
    storage_points,
    thousands,
    undismissed,
)
from mailarc_ui.dashboard.state import DashboardState

__all__ = [
    "DEFAULT_RANGE",
    "NOTIFICATION_LIMIT",
    "NOTIFICATION_SHOWN",
    "RANGE_DAYS",
    "UNKNOWN",
    "DashboardCounts",
    "DashboardState",
    "MeterView",
    "NotificationView",
    "Readout",
    "ServiceView",
    "VectorState",
    "chosen_range",
    "dashboard_panel",
    "day_label",
    "days_in",
    "dismissed_keys",
    "gigabytes",
    "health_meters",
    "human_bytes",
    "kpi_band",
    "last_archived_label",
    "messages_points",
    "moment_label",
    "notice_key",
    "notifications_of",
    "percent_label",
    "ratio_percent",
    "remembering",
    "services_of",
    "storage_meters",
    "storage_points",
    "thousands",
    "undismissed",
]
