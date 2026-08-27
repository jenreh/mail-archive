"""The frame every page of the archive is drawn inside.

Five files, one responsibility each: ``routes`` is where a path is spelled,
``model`` is what a navigation entry is, ``navigation`` renders the sidebar,
``templates`` holds the two layouts and the decorator a public page uses, and
``access`` answers the one question every state has to ask before it hands
anything over — a Reflex handler is reached by name over the websocket and not
by route, so a page's ``admin_only`` gate never sees it.

The split matters in one direction in particular. ``routes`` imports nothing,
so a page module can alias its route without pulling Reflex components in with
it, and the documentation check can walk the table without building a sidebar.
"""

from mailarc_ui.shell import routes
from mailarc_ui.shell.access import granted, signed_in_user
from mailarc_ui.shell.model import NavItem, NavSection
from mailarc_ui.shell.navigation import NAV_SECTIONS, NAVBAR_WIDTH, app_sidebar
from mailarc_ui.shell.templates import (
    PageContent,
    Template,
    mailarc_app,
    mailarc_full_app,
    public_page,
)

__all__ = [
    "NAVBAR_WIDTH",
    "NAV_SECTIONS",
    "NavItem",
    "NavSection",
    "PageContent",
    "Template",
    "app_sidebar",
    "granted",
    "mailarc_app",
    "mailarc_full_app",
    "public_page",
    "routes",
    "signed_in_user",
]
