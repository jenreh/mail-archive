"""Searching the archive — the address a person arrives at.

``/``, and that is the whole point of the redesign: what somebody opens a mail
archive to do is find a message, so the search is the front door and the
dashboard is one click away in the rail rather than the other way round.

On ``mailarc_full_app``: this page is a three-column reader — the form, the
result list, the message — and each column scrolls on its own, which needs a
parent with a definite height rather than one that grows to fit them.

No page header: the design carries no page chrome, and the three columns
take the whole window rather than the height left over under a heading.

Layout and nothing else. The whole panel is one component ``mailarc-ui``
exports — :func:`~mailarc_ui.search.search_panel` — and this module only gives
it a route, a title and that height. It will not grow logic of its own: a page
that starts computing something is a panel that has not been written yet.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import PAGE_INSET
from mailarc_ui.search import MailSearchState, search_panel
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_full_app, public_page

ROUTE = routes.SEARCH
"""Where this page lives; the rail reads the same constant."""


@public_page(
    route=ROUTE,
    title="Search",
    description="Find a message in the archive, and read it",
    template=mailarc_full_app,
    on_load=[MailSearchState.load],
)
def search_page() -> rx.Component:
    return mn.box(search_panel(), w="100%", h="100%", p=PAGE_INSET)
