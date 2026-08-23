"""What the analyses found, and whether the co-addressed edge can be believed.

Layout and nothing else. The whole body is one component ``mailarc-ui``
exports — :func:`insights_panel` — and this module only gives it a route, a
title and the sentence that says what a reader is looking at.

No height games, unlike ``/admin/review``. That page is a two-pane reader whose
halves scroll on their own, so it needs a definite height to divide; this one
is a column of tables that grows downwards, and the template's own scrolling
container already handles it. Fixing a height here would squeeze four panels
into one screenful and make each of them scroll separately.
"""

import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.templates import authenticated

from app.components.navbar import app_navbar
from mailarc_ui.insights import AnalyticsInsightsState, insights_panel

ROUTE = "/admin/insights"
"""Where this page lives; ``app/components/navbar.py`` links here."""


@authenticated(
    route=ROUTE,
    title="Insights",
    description="What a rebuild derived from the archive, and whether it holds up",
    navbar=app_navbar(),
    with_header=False,
    # Admin-only, for the reason `/admin/review` is: the analyses read every
    # mailbox of the installation, which is everybody's private mail, and a
    # co-recipient listing names who writes to whom.
    admin_only=True,
    # ty cannot model reflex event-handler calls; suppress the false positive.
    on_load=[AnalyticsInsightsState.load],  # ty: ignore[invalid-argument-type]
)
def mail_insights_page() -> rx.Component:
    return mn.stack(
        mn.stack(
            mn.title("Insights", order=1),
            mn.text(
                "What a rebuild made of the archive: who gets addressed "
                "together, which of those groups recur, what the mail is "
                "about and how much of it is machine-written. The cross-check "
                "recomputes the co-addressed counts from the messages "
                "themselves and holds them against the edge a rebuild wrote, "
                "so an edge that drifted says so here instead of quietly "
                "colouring every report below it.",
                c="dimmed",
                size="sm",
            ),
            gap="xs",
        ),
        insights_panel(),
        gap="lg",
        w="100%",
    )
