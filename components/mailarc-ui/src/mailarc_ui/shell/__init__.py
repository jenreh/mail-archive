"""The frame every page of the archive is drawn inside.

Four files, one responsibility each: ``routes`` is where a path is spelled,
``model`` is what a rail entry is, ``navigation`` renders the icon rail, and
``templates`` holds the two layouts and the decorator every page is registered
with.

There used to be a fifth. ``access`` answered "who is asking?" for the states,
because a Reflex handler is reached by name over the websocket rather than by
route and a page's gate never saw it. The archive has no sign-in any more, so
there is nobody to ask about and nothing to refuse; the file is gone rather
than left answering "yes" to every caller.

The split matters in one direction in particular. ``routes`` imports nothing,
so a page module can alias its route without pulling Reflex components in with
it, and the documentation check can walk the table without building a rail.
"""

from mailarc_ui.shell import routes
from mailarc_ui.shell.model import NavItem, NavSection
from mailarc_ui.shell.navigation import NAVBAR_WIDTH, RAIL_SECTIONS, app_sidebar
from mailarc_ui.shell.templates import (
    PageContent,
    Template,
    mailarc_app,
    mailarc_full_app,
    public_page,
)

__all__ = [
    "NAVBAR_WIDTH",
    "RAIL_SECTIONS",
    "NavItem",
    "NavSection",
    "PageContent",
    "Template",
    "app_sidebar",
    "mailarc_app",
    "mailarc_full_app",
    "public_page",
    "routes",
]
