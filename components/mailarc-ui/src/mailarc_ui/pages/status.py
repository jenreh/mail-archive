"""The live state of the graph server this archive is built on.

Was ``/`` — the Hello World page the project was generated with, which had
grown into a FalkorDB status panel and was still sitting on the address a
visitor arrives at. It moved to ``/admin/status`` so the dashboard can have
that address, and it is administration: an endpoint, a memory figure and a
node count say more about the installation than a user of the archive should
be shown.
"""

import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.templates import authenticated_page

from mailarc_ui.kit import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_app
from mailarc_ui.status import GraphStatusState, status_panel

ROUTE = routes.GRAPH_STATUS
"""Where this page lives; the sidebar reads the same constant."""


@authenticated_page(
    route=ROUTE,
    title="Graph status",
    description="The live state of the graph server this archive is built on",
    template=mailarc_app,
    # Administration, for the reason the docstring gives: this page names the
    # endpoint the graph answers at and how much of the machine it is using.
    admin_only=True,
    # ty cannot model reflex event-handler calls; suppress the false positive.
    on_load=[GraphStatusState.start_polling],  # ty: ignore[invalid-argument-type]
)
def graph_status_page() -> rx.Component:
    return mn.stack(
        page_header(
            "Graph status",
            "The desktop app runs this page and its FalkorDB from binaries "
            "bundled inside the app — nothing is installed on the machine.",
        ),
        status_panel(),
        gap=PAGE_GAP,
        w="100%",
        maw=900,
        mx="auto",
        p=PAGE_PADDING,
    )
