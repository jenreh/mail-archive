"""Every route this application serves, named once.

The single source of truth. A page aliases the constant it answers at into its
own ``ROUTE``, the navigation reads the same constants, and the documentation
check walks them — so a route cannot be changed in one of those three places
and left stale in the other two. That is not hypothetical: three admin pages
moved from ``/mail/*`` to ``/admin/*`` and six places went on naming the old
paths, because the routes were string literals living next to each other
rather than one name used three times.

Only routes a person navigates to. The login and password-reset pages appkit
registers are appkit's routes and it owns their spelling; naming them here
would create a second place they could disagree.
"""

DASHBOARD = "/"
"""The welcome dashboard. The one public page: no sign-in, no admin gate."""

REVIEW = "/admin/review"
INSIGHTS = "/admin/insights"
ACCOUNTS = "/admin/accounts"
EMBEDDER = "/admin/embedder"
GRAPH_STATUS = "/admin/status"
USERS = "/admin/users"
PROFILE = "/profile"

ALL_ROUTES: tuple[str, ...] = (
    DASHBOARD,
    REVIEW,
    INSIGHTS,
    ACCOUNTS,
    EMBEDDER,
    GRAPH_STATUS,
    USERS,
    PROFILE,
)
"""In the order the sidebar shows them, so a reader of either sees the same
application. A tuple because a route table is not something a caller edits."""

__all__ = [
    "ACCOUNTS",
    "ALL_ROUTES",
    "DASHBOARD",
    "EMBEDDER",
    "GRAPH_STATUS",
    "INSIGHTS",
    "PROFILE",
    "REVIEW",
    "USERS",
]
