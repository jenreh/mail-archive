"""What the analyses found, and whether the co-addressed edge can be believed.

Layout and nothing else. The whole body is one component ``mailarc-ui``
exports — :func:`insights_panel` — and this module only gives it a route, a
title and the sentence that says what a reader is looking at.

No height games, unlike ``/admin/review``. That page is a two-pane reader whose
halves scroll on their own, so it needs a definite height to divide; this one
is a column of tables that grows downwards, and the shell's own scrolling
container already handles it. Fixing a height here would squeeze four panels
into one screenful and make each of them scroll separately.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.insights import AnalyticsInsightsState, insights_panel
from mailarc_ui.kit import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_app, public_page

ROUTE = routes.INSIGHTS
"""Where this page lives; the rail reads the same constant."""


@public_page(
    route=ROUTE,
    title="Insights",
    description="What a rebuild derived from the archive, and whether it holds up",
    template=mailarc_app,
    on_load=[AnalyticsInsightsState.load],
)
def insights_page() -> rx.Component:
    return mn.stack(
        page_header(
            "Insights",
            "What a rebuild made of the archive: who gets addressed together, "
            "which of those groups recur, what the mail is about and how much "
            "of it is machine-written. The cross-check recomputes the "
            "co-addressed counts from the messages themselves and holds them "
            "against the edge a rebuild wrote, so an edge that drifted says so "
            "here instead of quietly colouring every report below it.",
        ),
        insights_panel(),
        gap=PAGE_GAP,
        w="100%",
        maw=1280,
        mx="auto",
        p=PAGE_PADDING,
    )
