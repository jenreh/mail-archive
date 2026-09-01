"""What the analyses found, and whether the co-addressed edge can be believed.

Layout and nothing else. The whole body is one component ``mailarc-ui``
exports — :func:`insights_panel` — and this module only gives it a route, a
title and the sentence that says what a reader is looking at.

No height games, unlike ``/``. That page is a three-pane reader whose
columns scroll on their own, so it needs a definite height to divide; this one
is two columns of cards that grow downwards, and the shell's own scrolling
container already handles them. Fixing a height here would squeeze four panels
into one screenful. What each *listing* does inside its card is a different
question, and :func:`~mailarc_ui.kit.scroll_table` answers it: twelve rows
under a pinned header, so a card is a fixed size whatever the ranking holds.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.insights import AnalyticsInsightsState, insights_panel
from mailarc_ui.kit import PAGE_GAP, PAGE_INSET
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
        insights_panel(),
        gap=PAGE_GAP,
        w="100%",
        p=PAGE_INSET,
    )
