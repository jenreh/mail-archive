"""Every route this application serves, named once.

The single source of truth. A page aliases the constant it answers at into its
own ``ROUTE``, the navigation reads the same constants, and the documentation
check walks them — so a route cannot be changed in one of those three places
and left stale in the other two. That is not hypothetical: three admin pages
moved from ``/mail/*`` to ``/admin/*`` and six places went on naming the old
paths, because the routes were string literals living next to each other
rather than one name used three times.

Only routes a person navigates to, and after the mail-client redesign that is
all of them: the archive is a desktop application with no sign-in, so there is
no longer a login page, a password reset or a user administration to name.
"""

SEARCH = "/"
"""Searching the archive — the address a person arrives at and works from."""

DASHBOARD = "/dashboard"
"""What the archive holds. Reachable from the rail, no longer the front door."""

INSIGHTS = "/insights"
"""What a rebuild derived from the archive. In the main menu, not under
``/admin/``: it is something a reader of the archive looks at rather than
something an operator maintains."""

ACCOUNTS = "/admin/accounts"
EMBEDDER = "/admin/embedder"
GRAPH_STATUS = "/admin/status"

ALL_ROUTES: tuple[str, ...] = (
    SEARCH,
    DASHBOARD,
    INSIGHTS,
    ACCOUNTS,
    EMBEDDER,
    GRAPH_STATUS,
)
"""In the order the rail shows them — the three menu items, then the three the
admin popover holds — so a reader of either sees the same application. A tuple
because a route table is not something a caller edits."""

__all__ = [
    "ACCOUNTS",
    "ALL_ROUTES",
    "DASHBOARD",
    "EMBEDDER",
    "GRAPH_STATUS",
    "INSIGHTS",
    "SEARCH",
]
