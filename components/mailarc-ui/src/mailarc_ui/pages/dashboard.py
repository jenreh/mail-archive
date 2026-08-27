"""The archive at a glance, at ``/`` — the one page that needs no sign-in.

A4: user pages need no login. This is the page that makes that true, and the
only one built with :func:`~mailarc_ui.shell.templates.public_page` — appkit's
``authenticated_page`` puts ``LoginState.check_auth`` in front of every
``on_load`` and cannot serve a page without a session.

**Public here means the page, not everything on it.** ``DashboardState.load``
runs for a signed-out visitor — appkit runs an ``on_load`` chain to the end
whatever ``check_auth`` returned, and there is no ``check_auth`` in this one at
all — so the split between what any visitor is shown and what only an
administrator is shown lives in that state, at the point where data leaves the
process. :mod:`mailarc_ui.dashboard.state` is where the line is drawn and why.

No search field and no filter button: §1.2 removed the header bar the reference
design shows, and a page-level search that only one page carried would read as
one that is broken on the others.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.dashboard import DashboardState, dashboard_panel
from mailarc_ui.kit import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import public_page

ROUTE = routes.DASHBOARD
"""Where this page lives; the sidebar reads the same constant."""

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
