"""The archive at a glance, at ``/dashboard``.

It used to be the front door. The redesign gave ``/`` to the search, which is
what somebody opening a mail archive actually came to do, and moved this page
one click away into the rail — a summary of what the archive holds is
something a person looks at now and then, not the thing they arrive for.

No search field and no filter button: the header bar the reference design
shows was dropped, and a page-level search that only one page carried would
read as one that is broken on the others. The search lives at ``/`` now, which
is where the rail sends anybody who wants it.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.dashboard import DashboardState, dashboard_panel
from mailarc_ui.kit import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import public_page

ROUTE = routes.DASHBOARD
"""Where this page lives; the rail reads the same constant."""

MAX_CONTENT_WIDTH = 1440
"""How wide the dashboard is allowed to get.

Wider than the other pages, which cap around 900–1200: this one is a grid of
six cards and two charts, and at three columns it needs the room. Capped all
the same, because a chart stretched across a 4K display is a line with its
ticks metres apart.
"""


@public_page(
    route=ROUTE,
    title="Dashboard",
    description="What this mail archive holds, and whether it is healthy",
    on_load=[DashboardState.load],
)
def dashboard_page() -> rx.Component:
    return mn.stack(
        page_header(
            "Dashboard",
            "What this archive holds, how it grew, and whether everything "
            "behind it is running.",
        ),
        dashboard_panel(),
        gap=PAGE_GAP,
        w="100%",
        maw=MAX_CONTENT_WIDTH,
        mx="auto",
        p=PAGE_PADDING,
    )
