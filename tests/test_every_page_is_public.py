"""No page of this archive asks who is looking. This is the file that pins it.

The requirement — the archive is a desktop application and has no sign-in —
has exactly one mechanical expression: no ``on_load`` chain Reflex was handed
contains ``LoginState.check_auth``. Everything else about the application
being open is a consequence of that. appkit's four page decorators all put
that handler first and it redirects to a sign-in page this application no
longer serves, so a page built with any of them is a page nobody ever sees,
however open the rest of it looks.

Its own file rather than a case in
``components/mailarc-ui/tests/test_ui_pages.py`` because it is not a claim
about a page — it is the claim that separates this application from the one
before it, and it should fail on its own line with its own name when somebody
swaps one decorator for another. It replaces ``test_dashboard_is_public.py``,
which asked the same question of ``/`` alone, back when ``/`` was the only
page that answered without a session.

The complement is asserted in the same walk: a page whose ``on_load`` was
dropped along with its gate is a page that opens blank and says nothing about
why, and it would sail through a check that only looked for a handler's
absence.
"""

from typing import Any

from reflex.page import DECORATED_PAGES

from mailarc_ui.pages import (  # noqa: F401  # imported for their route registration
    accounts,
    dashboard,
    embedder,
    insights,
    review,
    search,
    status,
)
from mailarc_ui.shell import routes

CHECK_AUTH = "check_auth"
"""What appkit puts in front of an authenticated page's ``on_load``.

Matched as a substring of the qualified name rather than as
``LoginState.check_auth`` in full: what survives the trip through Reflex's
registry is an ``EventHandler`` or an ``EventSpec``, the function behind
either is the only stable thing to compare, and a renamed *class* holding the
same handler would still lock every page in the application.
"""

WAIT_CURSOR = "LoadingState.set_is_loading"
"""The reset every page's chain ends with.

``theme_wrapper`` renders a wait cursor while ``LoadingState.is_loading`` is
true, so a page that lost this handler renders perfectly and keeps the cursor
for as long as the visitor stays. It is also the negative control for the
check above: it proves the walk below can see handlers at all, which a page
table read through the wrong registry could not.
"""

LOADERS: dict[str, tuple[str, ...]] = {
    routes.SEARCH: (),
    routes.DASHBOARD: ("DashboardState.load",),
    routes.INSIGHTS: ("AnalyticsInsightsState.load",),
    routes.REVIEW: ("MessageReviewState.load",),
    routes.ACCOUNTS: ("MailAccountState.load", "ImportJobState.refresh"),
    routes.EMBEDDER: ("EmbedderSettingsState.load",),
    routes.GRAPH_STATUS: ("GraphStatusState.start_polling",),
}
"""What each page asks for when it opens.

Removing a gate and removing the ``on_load`` it stood in front of are one edit
apart, and only one of them is wanted. ``/`` is empty on purpose: its body is
still a placeholder, and naming a handler here before it exists would be a
test written against a plan.
"""


def _registered() -> dict[str, dict[str, Any]]:
    """Every page Reflex holds, by route.

    Read out of ``DECORATED_PAGES`` rather than off the page modules, because
    that registry is what the application is actually served from — a module
    that registered a second page, or none, is invisible from the source.
    """
    return {
        kwargs["route"]: kwargs
        for pages in DECORATED_PAGES.values()
        for _, kwargs in pages
        if kwargs.get("route")
    }


def _handler_names(kwargs: dict[str, Any]) -> set[str]:
    """The handlers an ``on_load`` chain fires, by qualified name.

    Two shapes, because ``on_load`` holds both: an ``EventHandler`` carries its
    function directly, and an ``EventSpec`` — what
    ``LoadingState.set_is_loading(False)`` evaluates to — carries a handler
    that carries it.
    """
    names: set[str] = set()
    for handler in kwargs.get("on_load") or []:
        if (fn := getattr(handler, "fn", None)) is not None:
            names.add(fn.__qualname__)
        elif (spec := getattr(handler, "handler", None)) is not None:
            names.add(spec.fn.__qualname__)
    return names


def test_every_page_of_the_archive_is_registered() -> None:
    """The premise of everything below, so nothing can pass by naming nothing."""
    assert set(LOADERS) == set(routes.ALL_ROUTES)
    assert set(LOADERS) <= set(_registered())


def test_no_page_is_authentication_checked() -> None:
    """The requirement, as one assertion over the whole application.

    Every route, not only the ones this file names: a page added on one of
    appkit's decorators would be caught here even before anybody thought to
    list it.
    """
    checked = sorted(
        route
        for route, kwargs in _registered().items()
        if any(CHECK_AUTH in name for name in _handler_names(kwargs))
    )

    assert not checked, f"{checked} redirect to a sign-in this archive has not"


def test_every_page_still_primes_itself() -> None:
    """Open is not the same as inert.

    A decorator swap that dropped the whole chain would pass the test above
    and leave the pages rendering empty cards for ever.
    """
    registered = _registered()
    for route, loaders in LOADERS.items():
        fired = _handler_names(registered[route])
        assert WAIT_CURSOR in fired, f"{route} never clears the wait cursor"
        for loader in loaders:
            assert loader in fired, f"{route} never fires {loader}"
